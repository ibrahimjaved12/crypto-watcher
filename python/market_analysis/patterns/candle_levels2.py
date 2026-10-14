"""candle-table-v1 group levels2 (lv2-v1): liquidity pools, anchored VWAP, volume profile (docs/pattern-study.md).

Everything follows the open-time rule of candle_location: what a row reads must be known when its
candle opened (``open_ms(i)``); the row's own high, low, close and ``atr_prev`` are then compared
with it. Prices are scaled integers in storage; distances are in ``atr_prev`` units. Like
candle_location, this module reads a ``TableContext`` duck-typed and is wired in by candle_table.

- Liquidity pools: equal highs/lows. Fractal swings (n = 3) on the dk1 daily candles for
  15/60/240 rows, on the row's own candles for 1440/10080 rows. A new swing joins every active
  swing of the same side within 0.15 * ATR14 (the source series' ATR through the confirming
  candle); 2 or more members form a pool at their mean, ``tier`` = member count, known at the
  end of the confirming candle. A swing or pool is invalid after a source close beyond it by
  more than 0.1 * ATR14 (through that candle), 90 days after it became known, or (pool) when a
  newer pool takes over any of its members.
- Anchored VWAP: from the last same-tf fractal swing low / high (n = 5) confirmed by the end of
  candle i-1, sum(typical * volume) / sum(volume) over the swing candle through i-1, typical =
  (h + l + c) / 3. Empty if any of those candles is invalid or lacks volume. Intraday rows also
  get the UTC-day VWAP from the day start through i-1 (empty for the day's first candle).
- Volume profile: at every UTC day open, from the previous 30 / 90 complete days of 1m bars
  (2024+ only), on log-price bins of 10 bp: bin = floor(ln(close) / 0.001). Each minute's whole
  volume goes to the bin of its close (an approximation: the minute's range is ignored);
  minutes with a missing/compromised price or missing volume are skipped. POC = the
  max-volume bin (ties: the lower bin), reported at the bin centre. Value area: start at the
  POC and, while it holds less than 70 % of the volume, compare the two bins above with the
  two bins below (fewer at the edge of the profile) and add the side with more volume (ties:
  the upper side). VAH / VAL = the upper / lower edge of the value area. Valid for that day.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction
from math import exp, log

from .. import data_lake
from ..benchmark import levels as lv
from ..benchmark.bars import COMPROMISED_FLAGS, MISSING, BarSeries
from ..benchmark.candles import CandleSeries
from ..daily_lake import DAY_MS
from .candle_location import LevelIndex, TOUCH_BAND_ATR, _HTF_PARAMS
from .features import CandleFeatureHistory

SCALE = 10 ** data_lake.SCALE_DIGITS
POOL_N = 3
POOL_CLUSTER_ATR = 0.15
POOL_BREAK_ATR = 0.1
POOL_PIERCE_ATR = 0.1
POOL_VALID_DAYS = 90
AVWAP_N = 5
PROFILE_BIN = 0.001  # 10 bp in log price
PROFILE_WINDOWS = (30, 90)
VALUE_AREA = Fraction(7, 10)


# ---------------------------------------------------------------- liquidity pools


def _swings(candles: CandleSeries, n: int) -> dict:
    """{confirm index: [(side, price), ...]} of fractal swings (all 2n+1 candles valid, strict)."""
    out = defaultdict(list)
    high, low, valid = candles.high, candles.low, candles.valid
    for j in range(n, len(candles) - n):
        window = range(j - n, j + n + 1)
        if not all(valid[k] for k in window):
            continue
        others = [k for k in window if k != j]
        if all(high[j] > high[k] for k in others):
            out[j + n].append(("high", high[j]))
        if all(low[j] < low[k] for k in others):
            out[j + n].append(("low", low[j]))
    return out


def liquidity_pools(candles: CandleSeries, n: int = POOL_N) -> list:
    """Pools as ``levels.Level`` (set ``pool``, side ``high``/``low``, ``tier`` = member count)."""
    history = CandleFeatureHistory(candles, **_HTF_PARAMS)
    atr = [history.indicators(k)["atr"] for k in range(len(candles))]
    confirmations = _swings(candles, n)
    horizon = POOL_VALID_DAYS * DAY_MS
    pools = []
    for side in ("high", "low"):
        sign = 1 if side == "high" else -1
        active = []      # swings: [price, known_ms, member id]
        open_pools = []  # [record, member ids]
        next_id = 0
        for k in range(len(candles)):
            end = candles.end_ms[k]
            if candles.valid[k] and atr[k]:
                limit = POOL_BREAK_ATR * atr[k] * SCALE
                close = candles.close[k]
                active = [s for s in active if sign * (close - s[0]) <= limit]
                for pool in open_pools:
                    record = pool[0]
                    if record["valid_to"] is None and record["valid_from"] < end \
                            and sign * (close - record["price"]) > limit:
                        record["valid_to"] = end
            active = [s for s in active if end < s[1] + horizon]
            for swing_side, price in confirmations.get(k, ()):
                if swing_side != side:
                    continue
                new = [price, end, next_id]
                next_id += 1
                if atr[k]:
                    width = POOL_CLUSTER_ATR * atr[k] * SCALE
                    members = [s for s in active if abs(s[0] - price) <= width] + [new]
                    if len(members) >= 2:
                        ids = {s[2] for s in members}
                        for pool in open_pools:
                            if pool[0]["valid_to"] is None and pool[1] & ids:
                                pool[0]["valid_to"] = end  # a newer pool takes over its members
                        mean = Fraction(sum(s[0] for s in members), len(members))
                        open_pools.append([{"price": round(mean), "valid_from": end, "valid_to": None,
                                            "touches": len(members)}, ids])
                active.append(new)
        for record, _ in open_pools:
            stop = record["valid_from"] + horizon
            valid_to = stop if record["valid_to"] is None else min(record["valid_to"], stop)
            if valid_to > record["valid_from"]:
                pools.append(lv.Level("pool", side, record["price"], record["valid_from"], valid_to,
                                      record["touches"]))
    return pools


# ---------------------------------------------------------------- anchored VWAP


def anchored_vwap(candles: CandleSeries, first: int, stop: int):
    """VWAP (price, float) over candles ``first .. stop-1``; None if empty, invalid or volume missing."""
    if not 0 <= first < stop <= len(candles):
        return None
    total = weight = 0
    for k in range(first, stop):
        volume = candles.volume[k]
        if not candles.valid[k] or volume == MISSING:
            return None
        total += (candles.high[k] + candles.low[k] + candles.close[k]) * volume
        weight += volume
    return total / (3 * weight * SCALE) if weight > 0 else None


def _last_swing_index(candles: CandleSeries, n: int) -> tuple[list, list]:
    """Per row i: index of the last swing low / high confirmed by the end of i-1 (j + n <= i - 1)."""
    lows, highs = [None] * len(candles), [None] * len(candles)
    confirmations = _swings(candles, n)
    lo = hi = None
    for i in range(len(candles)):
        lows[i], highs[i] = lo, hi
        for side, _ in confirmations.get(i, ()):
            if side == "low":
                lo = i - n
            else:
                hi = i - n
    return lows, highs


# ---------------------------------------------------------------- volume profile


def _day_histograms(bars: BarSeries) -> list:
    """[(day_start, {bin: volume})] of the complete UTC days inside the bars, in order."""
    first = bars.start_ms + (-bars.start_ms % DAY_MS)
    out = []
    shift = log(SCALE)
    for day in range(first, bars.end_ms - DAY_MS + 1, DAY_MS):
        a = (day - bars.start_ms) // data_lake.MINUTE_MS
        hist = defaultdict(int)
        for m in range(a, a + 1440):
            close, volume = bars.close[m], bars.volume[m]
            if close == MISSING or close <= 0 or volume == MISSING or bars.flags[m] & COMPROMISED_FLAGS:
                continue
            hist[int((log(close) - shift) // PROFILE_BIN)] += volume
        out.append((day, dict(hist)))
    return out


def value_area(hist: dict):
    """(poc bin, low bin, high bin) of a {bin: volume} profile; None when it holds no volume."""
    total = sum(hist.values())
    if total <= 0:
        return None
    poc = min(hist, key=lambda b: (-hist[b], b))  # ties: the lower bin
    low = high = poc
    bottom, top = min(hist), max(hist)
    inside = hist[poc]
    while inside < VALUE_AREA * total:
        up = [b for b in (high + 1, high + 2) if b <= top]
        down = [b for b in (low - 1, low - 2) if b >= bottom]
        up_volume = sum(hist.get(b, 0) for b in up)
        down_volume = sum(hist.get(b, 0) for b in down)
        if up and (not down or up_volume >= down_volume):  # ties: the upper side
            high, inside = up[-1], inside + up_volume
        else:
            low, inside = down[-1], inside + down_volume
    return poc, low, high


def daily_profiles(bars: BarSeries, windows=PROFILE_WINDOWS) -> dict:
    """{day_start: {window: (poc, val, vah) prices as floats}} from the previous ``window`` complete days."""
    days = _day_histograms(bars)
    out = {}
    for window in windows:
        running = defaultdict(int)
        for t, (day, _) in enumerate(days):
            if t >= 1:
                for b, v in days[t - 1][1].items():
                    running[b] += v
            if t - 1 - window >= 0:
                for b, v in days[t - 1 - window][1].items():
                    running[b] -= v
                    if not running[b]:
                        del running[b]
            if t >= window and days[t - window][0] == day - window * DAY_MS:
                area = value_area(running)
                if area is not None:
                    poc, low, high = area
                    out.setdefault(day, {})[window] = (exp((poc + 0.5) * PROFILE_BIN), exp(low * PROFILE_BIN),
                                                       exp((high + 1) * PROFILE_BIN))
    return out


# ---------------------------------------------------------------- group


def prepare(ctx) -> None:
    """Attach pool indexes, swing anchors and VWAP inputs (called by TableContext.__post_init__)."""
    source = ctx.candles if ctx.minutes in (1440, 10080) else getattr(ctx, "history_days", None)
    if source is None:
        ctx.pool_index = None
    else:
        pools = liquidity_pools(source)
        ctx.pool_index = {side: LevelIndex([p for p in pools if p.side_hint == side]) for side in ("high", "low")}
    ctx.avwap_low_anchor, ctx.avwap_high_anchor = _last_swing_index(ctx.candles, AVWAP_N)


def _touch(price: float, low: float, high: float, atr: float) -> bool:
    return low - TOUCH_BAND_ATR * atr <= price <= high + TOUCH_BAND_ATR * atr


def levels2(ctx, i: int) -> dict:
    c = ctx.candles
    intraday = ctx.minutes in (15, 60, 240)
    atr = ctx.ind[i - 1]["atr"] if i else None
    names = [name for name, _ in LEVELS2_COLUMNS if intraday or name != "vwap_day_dist_atr"]
    if not atr or atr <= 0:
        return dict.fromkeys(names)
    out = {}
    close, high, low = c.close[i] / SCALE, c.high[i] / SCALE, c.low[i] / SCALE
    opened = c.open_ms(i)
    # pools
    if ctx.pool_index is None:
        out.update(dict.fromkeys(("pool_dist_up_atr", "pool_dist_dn_atr", "pool_touches_up", "pool_touches_dn",
                                  "touch_pool", "pool_swept")))
    else:
        usable = [p for side in ("high", "low") for p in ctx.pool_index[side].usable(opened)]
        above = sorted((p for p in usable if p.price / SCALE >= close), key=lambda p: (p.price, p.side_hint))
        below = sorted((p for p in usable if p.price / SCALE <= close), key=lambda p: (-p.price, p.side_hint))
        out["pool_dist_up_atr"] = (above[0].price / SCALE - close) / atr if above else None
        out["pool_touches_up"] = above[0].tier if above else None
        out["pool_dist_dn_atr"] = (close - below[0].price / SCALE) / atr if below else None
        out["pool_touches_dn"] = below[0].tier if below else None
        out["touch_pool"] = any(_touch(p.price / SCALE, low, high, atr) for p in usable)
        pierce = POOL_PIERCE_ATR * atr
        out["pool_swept"] = any(
            (high >= p.price / SCALE + pierce and close < p.price / SCALE) if p.side_hint == "high"
            else (low <= p.price / SCALE - pierce and close > p.price / SCALE) for p in usable)
    # anchored VWAP (anchors confirmed by the end of i-1; window anchor .. i-1)
    for label, anchor in (("lo", ctx.avwap_low_anchor[i]), ("hi", ctx.avwap_high_anchor[i])):
        vwap = anchored_vwap(c, anchor, i) if anchor is not None else None
        out[f"avwap_{label}_dist_atr"] = (vwap - close) / atr if vwap is not None else None
        out[f"touch_avwap_{label}"] = _touch(vwap, low, high, atr) if vwap is not None else None
    if intraday:
        first = (opened // DAY_MS * DAY_MS - c.start_ms) // (ctx.minutes * data_lake.MINUTE_MS)
        vwap = anchored_vwap(c, max(first, 0), i) if first >= 0 else None
        out["vwap_day_dist_atr"] = (vwap - close) / atr if vwap is not None else None
    # volume profile of the UTC day containing the candle's open
    profiles = getattr(ctx, "profiles", None) or {}
    day = profiles.get(opened // DAY_MS * DAY_MS, {})
    for window in PROFILE_WINDOWS:
        tag = f"{window}d"
        values = day.get(window)
        if values is None:
            out.update(dict.fromkeys((f"dist_poc_{tag}_atr", f"dist_vah_{tag}_atr", f"dist_val_{tag}_atr",
                                      f"in_value_area_{tag}", f"touch_poc_{tag}")))
            continue
        poc, val, vah = values
        out.update({f"dist_poc_{tag}_atr": (poc - close) / atr, f"dist_vah_{tag}_atr": (vah - close) / atr,
                    f"dist_val_{tag}_atr": (val - close) / atr, f"in_value_area_{tag}": val <= close <= vah,
                    f"touch_poc_{tag}": _touch(poc, low, high, atr)})
    return out


LEVELS2_COLUMNS = (
    ("pool_dist_up_atr", "(nearest usable liquidity pool >= close - close) / atr_prev"),
    ("pool_dist_dn_atr", "(close - nearest usable liquidity pool <= close) / atr_prev"),
    ("pool_touches_up", "member swings of that pool above"),
    ("pool_touches_dn", "member swings of that pool below"),
    ("touch_pool", "a usable pool lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
    ("pool_swept", "pierced a pool by >= 0.1 atr_prev and closed back on the other side"),
    ("avwap_lo_dist_atr", "signed (AVWAP from the last confirmed swing low through i-1 - close) / atr_prev"),
    ("avwap_hi_dist_atr", "signed (AVWAP from the last confirmed swing high through i-1 - close) / atr_prev"),
    ("touch_avwap_lo", "swing-low AVWAP lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
    ("touch_avwap_hi", "swing-high AVWAP lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
    ("vwap_day_dist_atr", "intraday: signed (UTC-day VWAP from the day start through i-1 - close) / atr_prev"),
) + tuple(
    item for window in PROFILE_WINDOWS for tag in (f"{window}d",) for item in (
        (f"dist_poc_{tag}_atr", f"signed ({window}-day volume-profile POC - close) / atr_prev (1m bars, 2024+)"),
        (f"dist_vah_{tag}_atr", f"signed ({window}-day value-area high - close) / atr_prev"),
        (f"dist_val_{tag}_atr", f"signed ({window}-day value-area low - close) / atr_prev"),
        (f"in_value_area_{tag}", f"VAL <= close <= VAH of the {window}-day profile"),
        (f"touch_poc_{tag}", f"{window}-day POC lies in [low - 0.25 atr_prev, high + 0.25 atr_prev]"),
    ))
