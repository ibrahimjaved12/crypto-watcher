"""candle-table-v1: one row per symbol x timeframe x completed candle, for EVERY candle (docs/pattern-study.md).

The table is the input of the pattern event study: it is descriptive, holds no signal, and
pattern hits are just filters over its columns. Rows are built by a pure row builder from a
group registry; the writer, manifest and loaders below are deterministic.

Point in time. The decision time of candle ``i`` is its ``end_ms``. Columns named ``*_prev``
read candles through ``i - 1`` only; plain names read candle ``i`` (and its past). Outcome
columns read candles ``i + 1 .. i + m`` only, never past the segment that contains the decision
time (the last segment ends where the hidden stretch begins). Floats are scaled integers / 1e8;
geometry ratios and flags come from exact integer differences as in ``features.py``. Empty means
unavailable and is never filled.

Sources. 15/60/240 candles come from the rd 1-minute bars (``build_candles``), 1440/10080 from
the dk1 daily klines (``candles_from_daily``). Intraday rows start 2024-01 (development to
2025-06, validation 2025-07..2025-12); daily/weekly rows start 2021-01 (development-ext to
2025-06, validation); 2020 is indicator warm-up only. ``through_month`` defaults to and may not
exceed 2025-12; hidden months are never opened.

Adding a group: define its columns (name, one-line doc) and a build function
``build(ctx, i) -> dict``; register it with ``REGISTRY.register(Group(...))``. A column left out
of the returned dict is not applicable to that row (empty, not missing); a column returned as
None is missing. Every feature group gets ``<group>_ok`` and ``<group>_missing``.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from dataclasses import dataclass, field
from math import isfinite, log, sqrt
from statistics import median
from typing import Callable

from .. import data_lake
from ..benchmark.bars import MISSING, BarSeries
from ..benchmark.candles import CandleSeries
from ..benchmark.canonical import content_hash
from ..benchmark.hidden_guard import require_months
from ..benchmark import regime as regime_v1
from ..benchmark.segments import daily_segment_bounds_ms, segment_bounds_ms
from ..benchmark.volatility import BLOCK_MINUTES, VAR_SCALE, build_variance, horizon_sigma
from ..daily_lake import DAY_MS, load_symbol_daily, load_symbol_funding_range
from . import candle_derivatives as dv
from . import candle_levels2 as lv2
from . import candle_location as loc
from .daily_candles import candles_from_daily
from .features import CandleFeatureHistory

SCHEMA = "candle-table-v1"
DIR_PREFIX = "ct1"
REPORT_ROOT = "reports/candle-table"
INTRADAY_TIMEFRAMES = (15, 60, 240)
DAILY_TIMEFRAMES = (1440, 10080)
TABLE_TIMEFRAMES = (*INTRADAY_TIMEFRAMES, *DAILY_TIMEFRAMES)
INTRADAY_FIRST_MONTH = "2024-01"
DAILY_FIRST_MONTH = "2021-01"
DAILY_WARMUP_MONTH = "2020-01"
LAST_MONTH = "2025-12"  # default and maximum through_month: the hidden stretch is never read
INTRADAY_SEGMENTS = ("development", "validation")
DAILY_SEGMENTS_USED = ("development-ext", "validation")

ATR_PERIOD, ADX_PERIOD, EMA_FAST, EMA_SLOW, LOOKBACK = 14, 14, 20, 50, 20
BB_PERIOD, BB_K = 20, 2
KC_ATR_PERIOD, KC_K = 20, 1.5
PCT_WINDOW = {15: 2880, 60: 720, 240: 180, 1440: 90, 10080: 52}
SLOT_DAYS, SLOT_MIN_VALUES = 20, 10
INTRADAY_SIGMA_HALF_LIFE_DAYS = 3  # = screen.SIGMA_HALF_LIFE_DAYS
INTRADAY_SIGMA_MODEL = "ewma-3d-5m-v1"
DAILY_SIGMA_HALF_LIFE_DAYS = 30
DAILY_SIGMA_MIN_RETURNS = 30
DAILY_SIGMA_MODEL = "ewma-30d-daily-v1"
FORWARD_CANDLES = (1, 2, 4)
FUNDING_HOURS = (0, 8, 16)
SESSIONS = ((0, 8, "asia"), (8, 13, "europe"), (13, 21, "us"), (21, 24, "late"))

SCALE = 10 ** data_lake.SCALE_DIGITS
MINUTE = data_lake.MINUTE_MS
HOUR = 60 * MINUTE
_FUNDING_STEP = 8 * HOUR


class CandleTableError(ValueError):
    """Invalid table input (timeframe, month, registry)."""


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Group:
    id: str
    version: str
    columns: tuple  # ((name, one-line doc), ...)
    build: Callable | None  # build(ctx, i) -> dict; None for the key group (filled by the row builder)
    flags: bool = True      # <id>_ok and <id>_missing columns

    @property
    def names(self) -> tuple:
        return tuple(name for name, _ in self.columns)

    @property
    def flag_columns(self) -> tuple:
        return (f"{self.id}_ok", f"{self.id}_missing") if self.flags else ()


class GroupRegistry:
    """Ordered groups; a column name (including the flag columns) may appear only once."""

    def __init__(self):
        self._groups: list[Group] = []
        self._columns: set[str] = set()

    def register(self, group: Group) -> Group:
        if any(existing.id == group.id for existing in self._groups):
            raise CandleTableError(f"duplicate group id {group.id!r}")
        names = [*group.names, *group.flag_columns]
        for name, doc in group.columns:
            if not name or not isinstance(doc, str) or not doc.strip():
                raise CandleTableError(f"column {name!r} of group {group.id!r} needs a one-line doc")
        duplicated = sorted({name for name in names if names.count(name) > 1} | (set(names) & self._columns))
        if duplicated:
            raise CandleTableError(f"group {group.id!r} repeats columns: {', '.join(duplicated)}")
        self._groups.append(group)
        self._columns.update(names)
        return group

    @property
    def groups(self) -> tuple:
        return tuple(self._groups)

    def columns(self) -> tuple:
        return tuple(name for group in self._groups for name in (*group.names, *group.flag_columns))

    def docs(self) -> list:
        out = []
        for group in self._groups:
            out.extend({"name": name, "group": group.id, "doc": doc} for name, doc in group.columns)
            for name in group.flag_columns:
                doc = ("1 if every applicable column of the group is filled" if name.endswith("_ok")
                       else "applicable columns of the group that are empty, '|'-joined")
                out.append({"name": name, "group": group.id, "doc": doc})
        return out

    def versions(self) -> dict:
        return {group.id: group.version for group in self._groups}


# ---------------------------------------------------------------- inputs of one (symbol, timeframe)


@dataclass(frozen=True)
class RegimeJoin:
    """regime-v1 label columns on a day grid starting at ``first_ms`` (``regime.build_table`` output)."""

    first_ms: int
    days: int
    market: dict   # column -> list[str] per day
    symbols: dict  # symbol -> {column -> list[str] per day}

    @classmethod
    def from_build_table(cls, columns: dict, first_ms: int, end_ms: int) -> RegimeJoin:
        return cls(first_ms, (end_ms - first_ms) // DAY_MS, columns["market"], columns["symbols"])

    def row(self, symbol: str, decision_ms: int) -> dict | None:
        """Labels of the UTC day containing ``decision_ms`` (a decision at midnight takes the new day,
        whose labels use closes through the previous day); None outside the grid."""
        day = decision_ms // DAY_MS - self.first_ms // DAY_MS
        if not 0 <= day < self.days or symbol not in self.symbols:
            return None
        values = {f"rg_{name}": column[day] for name, column in self.market.items()}
        values.update({f"rg_{name}": column[day] for name, column in self.symbols[symbol].items()})
        return values


def table_segments(minutes: int, through_month: str = LAST_MONTH) -> tuple:
    """((name, first_ms, end_ms), ...) of the timeframe's segments, clipped to the end of through_month."""
    end = data_lake.month_bounds_ms(check_through_month(through_month))[1]
    if minutes in INTRADAY_TIMEFRAMES:
        bounds = [(name, *segment_bounds_ms(name)) for name in INTRADAY_SEGMENTS]
    elif minutes in DAILY_TIMEFRAMES:
        bounds = [(name, *daily_segment_bounds_ms(name)) for name in DAILY_SEGMENTS_USED]
    else:
        raise CandleTableError(f"timeframe must be one of {TABLE_TIMEFRAMES}")
    return tuple((name, first, min(stop, end)) for name, first, stop in bounds if first < end)


def _f(value: int) -> float:
    return value / SCALE


def trailing_rank(values: list, window: int) -> list:
    """out[j] = fraction of values[j-window+1..j] that are <= values[j]; None unless all ``window`` exist."""
    out = [None] * len(values)
    ordered, missing = [], 0
    for j, value in enumerate(values):
        if value is None:
            missing += 1
        else:
            insort(ordered, value)
        if j >= window:
            old = values[j - window]
            if old is None:
                missing -= 1
            else:
                del ordered[bisect_left(ordered, old)]
        if j >= window - 1 and not missing:
            out[j] = bisect_right(ordered, value) / window
    return out


def intraday_sigma(bars: BarSeries, candles: CandleSeries, variance=None) -> list:
    """One-candle sigma at the end of each candle (screen.py rule): horizon_sigma(var[block], tf) / VAR_SCALE,
    block = (last minute index + 1) // 5 - 1 with the 3-day EWMA of 5-minute returns."""
    if variance is None:
        variance = build_variance(bars, INTRADAY_SIGMA_HALF_LIFE_DAYS).variance
    out = []
    for end in candles.end_ms:
        last = (end - bars.start_ms) // MINUTE - 1
        block = (last + 1) // BLOCK_MINUTES - 1
        var = variance[block] if 0 <= block < len(variance) else MISSING
        out.append(horizon_sigma(var, candles.minutes) / VAR_SCALE if var != MISSING and var > 0 else None)
    return out


def daily_sigma(days: CandleSeries) -> list:
    """EWMA (half-life 30 days, bias-corrected) of squared daily log returns through each day INCLUSIVE.

    A return exists between two consecutive price-valid days; a day without one changes nothing
    and carries the last published value. Published after 30 returns."""
    lam = 0.5 ** (1 / DAILY_SIGMA_HALF_LIFE_DAYS)
    a = w = 0.0
    n = 0
    published = None
    out = []
    for j in range(len(days)):
        if j and days.valid[j] and days.valid[j - 1]:
            r = log(days.close[j] / days.close[j - 1])
            a, w, n = lam * a + r * r, lam * w + 1.0, n + 1
            published = sqrt(a / w) if n >= DAILY_SIGMA_MIN_RETURNS else None
        out.append(published if days.valid[j] else None)
    return out


def weekly_sigma(days: CandleSeries, weeks: CandleSeries, sigma_days: list | None = None) -> list:
    """Daily sigma at the week's last day (Sunday) times sqrt(7)."""
    sigma_days = daily_sigma(days) if sigma_days is None else sigma_days
    out = []
    for i in range(len(weeks)):
        day = (weeks.open_ms(i) + 6 * DAY_MS - days.start_ms) // DAY_MS
        value = sigma_days[day] if 0 <= day < len(sigma_days) else None
        out.append(value * sqrt(7) if value is not None else None)
    return out


@dataclass
class TableContext:
    """Everything a group build function may read for one (symbol, timeframe)."""

    symbol: str
    minutes: int
    candles: CandleSeries
    sigma: list
    sigma_model: str
    segments: tuple
    regime: RegimeJoin | None = None
    levels: dict = field(default_factory=dict)  # level set -> [Level] (candle_location.context_levels)
    htf: dict = field(default_factory=dict)     # higher minutes -> CandleSeries (candle_location.htf_sources)
    history_days: CandleSeries | None = None    # dk1 1440 candles: ATH/ATL seed, intraday liquidity pools
    profiles: dict | None = None                # candle_levels2.daily_profiles of the symbol's 1m bars
    derivs: object | None = None                # candle_derivatives.DerivInputs (funding, metrics, basis)
    history: CandleFeatureHistory = field(init=False)
    ind: list = field(init=False)         # indicators through candle j (inclusive)
    streak: list = field(init=False)
    atr_rank: list = field(init=False)
    bb_rank: list = field(init=False)
    squeeze: list = field(init=False)
    squeeze_count: list = field(init=False)
    taker_ratio: list = field(init=False)
    level_index: dict = field(init=False)
    htf_state: dict = field(init=False)
    ath: list = field(init=False)
    atl: list = field(init=False)
    ath_before: list = field(init=False)
    atl_before: list = field(init=False)
    pool_index: dict | None = field(init=False)
    avwap_low_anchor: list = field(init=False)
    avwap_high_anchor: list = field(init=False)
    vwap_prefix: object = field(init=False)
    prem: list = field(init=False)
    prem_z: list = field(init=False)
    oi_end: list = field(init=False)
    oi_start: list = field(init=False)
    doi: list = field(init=False)
    doi_z: list = field(init=False)

    def __post_init__(self):
        candles = self.candles
        if self.minutes != candles.minutes or self.symbol != candles.symbol:
            raise CandleTableError("context symbol/timeframe differ from the candles")
        if len(self.sigma) != len(candles):
            raise CandleTableError("sigma must hold one value per candle")
        self.history = CandleFeatureHistory(candles, atr_period=ATR_PERIOD, adx_period=ADX_PERIOD,
                                            ema_fast_period=EMA_FAST, ema_slow_period=EMA_SLOW, lookback=LOOKBACK)
        count = len(candles)
        self.ind = [self.history.indicators(j) for j in range(count)]
        atr20 = self.history.wilder_atr(KC_ATR_PERIOD)
        self.streak, self.squeeze, self.squeeze_count = [None] * count, [None] * count, [None] * count
        widths, self.taker_ratio = [None] * count, [None] * count
        run = 0  # length of the valid run ending at j
        for j in range(count):
            if not candles.valid[j]:
                run = 0
                continue
            run += 1
            color = _sign(candles.close[j] - candles.open[j])
            previous = self.streak[j - 1] if j and candles.valid[j - 1] else None
            same = previous is not None and color != 0 and _sign(previous) == color
            self.streak[j] = previous + color if same else color
            volume, taker = candles.volume[j], candles.taker_buy_volume[j]
            if volume != MISSING and taker != MISSING and volume > 0:
                self.taker_ratio[j] = taker / volume
            if run >= BB_PERIOD:
                closes = [_f(value) for value in candles.close[j - BB_PERIOD + 1:j + 1]]
                mean = sum(closes) / BB_PERIOD
                std = sqrt(sum((value - mean) ** 2 for value in closes) / BB_PERIOD)
                widths[j] = 2 * BB_K * std / mean if mean > 0 else None
                ema, atr = self.ind[j]["ema_fast"], atr20[j]
                if ema is not None and atr is not None:
                    on = mean + BB_K * std < ema + KC_K * atr and mean - BB_K * std > ema - KC_K * atr
                    self.squeeze[j] = on
                    before = self.squeeze_count[j - 1] if j else None
                    self.squeeze_count[j] = ((before or 0) + 1) if on else 0
        window = PCT_WINDOW[self.minutes]
        self.atr_rank = trailing_rank([value["atr"] for value in self.ind], window)
        self.bb_rank = trailing_rank(widths, window)
        loc.prepare(self)
        lv2.prepare(self)
        dv.prepare(self)

    @property
    def intraday(self) -> bool:
        return self.minutes in INTRADAY_TIMEFRAMES

    def segment_of(self, decision_ms: int):
        for name, first, end in self.segments:
            if first <= decision_ms < end:
                return name, first, end
        return None


def _sign(value) -> int:
    return (value > 0) - (value < 0)


def _ratio(numerator, denominator):
    if numerator is None or denominator is None or not denominator > 0:
        return None
    value = numerator / denominator
    return value if isfinite(value) else None


# ---------------------------------------------------------------- groups


def _geometry(ctx: TableContext, i: int) -> dict:
    c = ctx.candles
    o, h, l, cl = c.open[i], c.high[i], c.low[i], c.close[i]
    span, body = h - l, abs(cl - o)
    upper, lower = h - max(o, cl), min(o, cl) - l
    atr = ctx.ind[i - 1]["atr"] if i else None
    out = {"open": _f(o), "high": _f(h), "low": _f(l), "close": _f(cl), "range": _f(span), "body": _f(body),
           "upper": _f(upper), "lower": _f(lower), "color": _sign(cl - o), "atr_prev": atr,
           "body_ratio": None, "upper_ratio": None, "lower_ratio": None, "doji_b05": None, "doji_b10": None}
    if span > 0:
        out.update(body_ratio=body / span, upper_ratio=upper / span, lower_ratio=lower / span,
                   doji_b05=100 * body <= 5 * span, doji_b10=10 * body <= span)
        if out["doji_b10"]:  # exact integer thresholds; empty (not applicable) on other candles
            if 10 * lower >= 6 * span and 10 * upper <= span:
                out["doji_subtype"] = "dragonfly"
            elif 10 * upper >= 6 * span and 10 * lower <= span:
                out["doji_subtype"] = "gravestone"
            elif 10 * upper >= 3 * span and 10 * lower >= 3 * span:
                out["doji_subtype"] = "long_legged"
            else:
                out["doji_subtype"] = "standard"
    range_atr = _ratio(_f(span), atr)
    out.update(range_atr=range_atr, body_atr=_ratio(_f(body), atr),
               size_ge_05=None if range_atr is None else range_atr >= 0.5,
               size_ge_10=None if range_atr is None else range_atr >= 1.0)
    return out


def _trend_vol(ctx: TableContext, i: int) -> dict:
    c = ctx.candles
    prev = ctx.ind[i - 1] if i else dict.fromkeys(("atr", "ema_fast", "ema_slow", "adx", "plus_di", "minus_di"))
    atr, fast, slow = prev["atr"], prev["ema_fast"], prev["ema_slow"]
    # _prev ratios use the sigma at the end of candle i - 1 so that they read nothing of candle i.
    sigma_prev = ctx.sigma[i - 1] if i else None
    ret1 = ret3 = extension = None
    if i >= 1 and c.valid[i - 1]:
        if sigma_prev:
            ret1 = log(c.close[i - 1] / c.open[i - 1]) / sigma_prev
        if fast is not None:
            extension = _ratio(_f(c.close[i - 1]) - fast, atr) if atr else None
    if i >= 3 and all(c.valid[i - 3:i]) and sigma_prev:
        ret3 = log(c.close[i - 1] / c.open[i - 3]) / sigma_prev
    both = fast is not None and slow is not None
    return {"ema20_prev": fast, "ema50_prev": slow, "trend_dir": _sign(fast - slow) if both else None,
            "ema_gap_atr_prev": _ratio(fast - slow, atr) if both else None, "adx_prev": prev["adx"],
            "plus_di_prev": prev["plus_di"], "minus_di_prev": prev["minus_di"], "extension_prev_atr": extension,
            "streak_prev": ctx.streak[i - 1] if i else None, "sigma_tf": ctx.sigma[i], "sigma_model": ctx.sigma_model,
            "ret1_prev_sigma": ret1, "ret3_prev_sigma": ret3,
            "atr_pct_prev": ctx.atr_rank[i - 1] if i else None, "bb_width_pct_prev": ctx.bb_rank[i - 1] if i else None,
            "squeeze_on_prev": ctx.squeeze[i - 1] if i else None,
            "squeeze_bars_prev": ctx.squeeze_count[i - 1] if i else None}


def _volume(ctx: TableContext, i: int) -> dict:
    c = ctx.candles
    volume = c.volume[i]
    out = {"volume": None if volume == MISSING else _f(volume),
           "rel_volume_prior20": ctx.history.at(i, include_adx=False).rel_volume}
    if ctx.intraday:
        per_day = 1440 // ctx.minutes
        values = [c.volume[j] for j in (i - k * per_day for k in range(1, SLOT_DAYS + 1))
                  if j >= 0 and c.valid[j] and c.volume[j] != MISSING]
        reference = median(values) if len(values) >= SLOT_MIN_VALUES else None
        out["rel_volume_slot"] = (volume / reference if volume != MISSING and reference and reference > 0
                                  else None)
    ratio = ctx.taker_ratio[i]
    prior = ctx.taker_ratio[i - LOOKBACK:i] if i >= LOOKBACK else []
    out.update(taker_buy_ratio=ratio,
               taker_ratio_prior20=sum(prior) / LOOKBACK if prior and None not in prior else None,
               taker_imb=None if ratio is None else 2 * ratio - 1)
    return out


def _calendar(ctx: TableContext, i: int) -> dict:
    open_ms, decision = ctx.candles.open_ms(i), ctx.candles.end_ms[i]
    dow = (open_ms // DAY_MS + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
    out = {"utc_dow": dow}
    if ctx.intraday:
        hour = (open_ms // HOUR) % 24
        first_settlement = -(-open_ms // _FUNDING_STEP) * _FUNDING_STEP  # first 00/08/16 UTC at or after open
        next_settlement = -(-decision // _FUNDING_STEP) * _FUNDING_STEP
        out.update(utc_hour=hour, is_weekend=dow >= 5,
                   session_bin=next(name for low, high, name in SESSIONS if low <= hour < high),
                   funding_in_candle=first_settlement < decision,
                   mins_to_funding=(next_settlement - decision) // MINUTE)
    return out


def _regime(ctx: TableContext, i: int) -> dict:
    values = ctx.regime.row(ctx.symbol, ctx.candles.end_ms[i]) if ctx.regime is not None else None
    if values is None:
        return dict.fromkeys(REGIME_COLUMNS)
    return {name: (values.get(name) or None) for name in REGIME_COLUMNS}


def _forward_window(ctx: TableContext, i: int, m: int) -> str:
    c = ctx.candles
    segment = ctx.segment_of(c.end_ms[i])
    if segment is None or i + m >= len(c) or c.end_ms[i + m] > segment[2]:
        return "past_end"  # checked before any candle of the window is read
    if not all(c.valid[i + 1:i + m + 1]):
        return "window_invalid"
    return "ok"


def _outcomes(ctx: TableContext, i: int) -> dict:
    c = ctx.candles
    sigma = ctx.sigma[i]
    out = {}
    first_status = None
    for m in FORWARD_CANDLES:
        status = _forward_window(ctx, i, m)
        first_status = first_status or status
        ok = status == "ok"
        entry = c.open[i + 1] if ok else None
        r = log(c.close[i + m] / entry) if ok else None
        out[f"ret_{m}_bp"] = 1e4 * r if ok else None
        out[f"ret_{m}_sigma"] = r / sigma if ok and sigma else None
        out[f"mfe_long_{m}_sigma"] = (log(max(c.high[i + 1:i + m + 1]) / entry) / sigma) if ok and sigma else None
        out[f"mae_long_{m}_sigma"] = (log(entry / min(c.low[i + 1:i + m + 1])) / sigma) if ok and sigma else None
        out[f"fwd_ok_{m}"] = ok and bool(sigma)
    if first_status == "ok":
        first_status = "ok" if sigma else "sigma_missing"
    one = first_status == "ok"
    range1 = log(c.high[i + 1] / c.low[i + 1]) / sigma if one else None
    out.update(range1_sigma=range1,
               absret1_sigma=abs(log(c.close[i + 1] / c.open[i + 1])) / sigma if one else None,
               ln_range1_sigma=log(range1) if range1 else None, fwd_status=first_status)
    return out


KEY_COLUMNS = (
    ("symbol", "Binance USD-M symbol"),
    ("tf", "candle length in minutes (15, 60, 240, 1440, 10080)"),
    ("open_ms", "candle open time, UTC epoch ms"),
    ("decision_ms", "candle end = decision time, UTC epoch ms"),
    ("segment", "segment containing decision_ms (development, development-ext, validation)"),
    ("status", "ok | invalid (candle price-invalid, nothing else filled) | insufficient (a feature group lacks history)"),
    ("reasons", "';'-joined: invalid_candle, or the feature groups that are not ok"),
)
GEOMETRY_COLUMNS = (
    ("open", "candle open price"), ("high", "candle high"), ("low", "candle low"), ("close", "candle close"),
    ("range", "high - low"), ("body", "|close - open|"), ("upper", "high - max(open, close)"),
    ("lower", "min(open, close) - low"), ("body_ratio", "body / range"), ("upper_ratio", "upper / range"),
    ("lower_ratio", "lower / range"), ("color", "+1 close > open, -1 close < open, 0 equal"),
    ("atr_prev", "Wilder ATR14 through candle i-1"), ("range_atr", "range / atr_prev"),
    ("body_atr", "body / atr_prev"), ("doji_b05", "body_ratio <= 0.05 (exact integers)"),
    ("doji_b10", "body_ratio <= 0.10 (exact integers)"),
    ("doji_subtype", "only when doji_b10: dragonfly | gravestone | long_legged | standard"),
    ("size_ge_05", "range_atr >= 0.5"), ("size_ge_10", "range_atr >= 1.0"),
)
TREND_VOL_COLUMNS = (
    ("ema20_prev", "EMA20 of closes through i-1"), ("ema50_prev", "EMA50 of closes through i-1"),
    ("trend_dir", "sign(ema20_prev - ema50_prev)"), ("ema_gap_atr_prev", "(ema20_prev - ema50_prev) / atr_prev"),
    ("adx_prev", "Wilder ADX14 through i-1"), ("plus_di_prev", "+DI14 through i-1"),
    ("minus_di_prev", "-DI14 through i-1"), ("extension_prev_atr", "(close_{i-1} - ema20_prev) / atr_prev"),
    ("streak_prev", "signed count of same-colour candles ending at i-1 (0 if i-1 is flat)"),
    ("sigma_tf", "one-candle log-return sigma at the end of candle i"),
    ("sigma_model", "ewma-3d-5m-v1 (intraday) or ewma-30d-daily-v1 (daily, weekly)"),
    ("ret1_prev_sigma", "ln(close_{i-1} / open_{i-1}) in units of the one-candle sigma at the end of i-1"),
    ("ret3_prev_sigma", "ln(close_{i-1} / open_{i-3}) in units of the one-candle sigma at the end of i-1 "
                        "(ret3 is not divided by sqrt(3))"),
    ("atr_pct_prev", "fraction of the W ATR14 values of candles i-W..i-1 that are <= atr_prev (full window)"),
    ("bb_width_pct_prev", "same percentile of BB(20,2) width = 4 * population std / SMA20, at i-1"),
    ("squeeze_on_prev", "BB(20,2) inside Keltner EMA20 +/- 1.5 * Wilder ATR20 at i-1"),
    ("squeeze_bars_prev", "consecutive candles with squeeze on ending at i-1"),
)
VOLUME_COLUMNS = (
    ("volume", "base volume of candle i"),
    ("rel_volume_prior20", "volume / mean volume of candles i-20..i-1 (features.py rule)"),
    ("rel_volume_slot", "intraday: volume / median volume of the same UTC slot over the previous 20 days (>= 10)"),
    ("taker_buy_ratio", "taker-buy volume / volume of candle i"),
    ("taker_ratio_prior20", "mean taker_buy_ratio of candles i-20..i-1"),
    ("taker_imb", "2 * taker_buy_ratio - 1"),
)
CALENDAR_COLUMNS = (
    ("utc_dow", "UTC weekday of the open, Monday = 0"), ("utc_hour", "intraday: UTC hour of the open"),
    ("is_weekend", "intraday: open on Saturday or Sunday UTC"),
    ("session_bin", "intraday: asia [0,8) europe [8,13) us [13,21) late [21,24) by open hour"),
    ("funding_in_candle", "intraday: a 00/08/16 UTC settlement lies in [open, decision)"),
    ("mins_to_funding", "intraday: minutes from decision to the next settlement (0 if at one)"),
)


REGIME_COLUMN_DOCS = tuple((f"rg_{name}", f"regime-v1 {name} of the UTC day containing decision_ms")
                           for name in (*regime_v1.MARKET_COLUMNS, *regime_v1.SYMBOL_COLUMNS))
REGIME_COLUMNS = tuple(name for name, _ in REGIME_COLUMN_DOCS)
_M_NOTE = ("", "; m-candle quantities are in one-candle sigma units, divide by sqrt(m) to compare across m")
OUTCOME_COLUMNS = tuple(
    item for m in FORWARD_CANDLES for item in (
        (f"ret_{m}_bp", f"1e4 * ln(close_(i+{m}) / open_(i+1))"),
        (f"ret_{m}_sigma", f"ln(close_(i+{m}) / open_(i+1)) / sigma_tf (one-candle sigma){_M_NOTE[m > 1]}"),
        (f"mfe_long_{m}_sigma", f"ln(max high of i+1..i+{m} / open_(i+1)) / sigma_tf (short MAE){_M_NOTE[m > 1]}"),
        (f"mae_long_{m}_sigma", f"ln(open_(i+1) / min low of i+1..i+{m}) / sigma_tf (short MFE){_M_NOTE[m > 1]}"),
        (f"fwd_ok_{m}", f"window i+1..i+{m} price-valid, inside the decision's segment, sigma present"),
    )) + (
    ("range1_sigma", "ln(high_(i+1) / low_(i+1)) / sigma_tf"),
    ("absret1_sigma", "|ln(close_(i+1) / open_(i+1))| / sigma_tf"),
    ("ln_range1_sigma", "ln(range1_sigma)"),
    ("fwd_status", "ok | window_invalid | past_end | sigma_missing (of the one-candle window)"),
)

GROUP_VERSIONS = {"keys": "keys-v1", "geometry": "geo-v1", "trend_vol": "tv-v1", "volume": "vol-v1",
                  "calendar": "cal-v1", "regime": "regime-v1", "location": "loc-v1", "sweep": "sweep-v1",
                  "htf": "htf-v1", "levels2": "lv2-v1",
                  "derivatives": "deriv-v1", "flush": "flush-v1",
                  "outcomes": "fwd-v1"}
FEATURE_GROUPS_FOR_STATUS = ("geometry", "trend_vol", "volume", "calendar", "regime")


def default_registry() -> GroupRegistry:
    registry = GroupRegistry()
    registry.register(Group("keys", GROUP_VERSIONS["keys"], KEY_COLUMNS, None, flags=False))
    for group_id, columns, build in (("geometry", GEOMETRY_COLUMNS, _geometry),
                                     ("trend_vol", TREND_VOL_COLUMNS, _trend_vol),
                                     ("volume", VOLUME_COLUMNS, _volume),
                                     ("calendar", CALENDAR_COLUMNS, _calendar),
                                     ("regime", REGIME_COLUMN_DOCS, _regime),
                                     ("location", loc.LOCATION_COLUMNS, loc.location),
                                     ("sweep", loc.SWEEP_COLUMNS, loc.sweep),
                                     ("htf", loc.HTF_COLUMNS, loc.htf),
                                     ("levels2", lv2.LEVELS2_COLUMNS, lv2.levels2),
                                     ("derivatives", dv.DERIVATIVES_COLUMNS, dv.derivatives),
                                     ("flush", dv.FLUSH_COLUMNS, dv.flush),
                                     ("outcomes", OUTCOME_COLUMNS, _outcomes)):
        registry.register(Group(group_id, GROUP_VERSIONS[group_id], columns, build))
    return registry


REGISTRY = default_registry()


# ---------------------------------------------------------------- rows


def build_rows(ctx: TableContext, registry: GroupRegistry = REGISTRY):
    """Rows (tuples in ``registry.columns()`` order) of every candle whose decision time lies in a segment."""
    c = ctx.candles
    columns = registry.columns()
    for i in range(len(c)):
        segment = ctx.segment_of(c.end_ms[i])
        if segment is None:
            continue
        row = dict.fromkeys(columns)
        row.update(symbol=ctx.symbol, tf=ctx.minutes, open_ms=c.open_ms(i), decision_ms=c.end_ms[i],
                   segment=segment[0])
        if not c.valid[i]:
            row.update(status="invalid", reasons="invalid_candle")
            yield tuple(row[name] for name in columns)
            continue
        not_ok = []
        for group in registry.groups:
            if group.build is None:
                continue
            values = group.build(ctx, i)
            unknown = set(values) - set(group.names)
            if unknown:
                raise CandleTableError(f"group {group.id} built undeclared columns: {sorted(unknown)}")
            row.update(values)
            missing = [name for name in group.names if name in values and values[name] is None]
            if group.flags:
                row[f"{group.id}_ok"] = not missing
                row[f"{group.id}_missing"] = "|".join(missing)
            if missing and group.id in FEATURE_GROUPS_FOR_STATUS:
                not_ok.append(group.id)
        row.update(status="insufficient" if not_ok else "ok", reasons=";".join(not_ok))
        yield tuple(row[name] for name in columns)


def cell(value) -> str:
    if value is None:
        return ""
    if value is True or value is False:
        return "1" if value else "0"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return repr(value) if isfinite(value) else ""
    if type(value) is str:
        if any(ch in value for ch in ",\r\n\""):
            raise CandleTableError(f"text cell {value!r} would need quoting")
        return value
    raise CandleTableError(f"unsupported cell type {type(value).__name__}")


def write_table(fileobj, rows, columns=None, counts: dict | None = None) -> int:
    """Deterministic csv.gz (header first). ``counts`` (optional) accumulates status/segment/fwd_status counts."""
    columns = tuple(columns or REGISTRY.columns())
    where = {name: columns.index(name) for name in ("symbol", "tf", "status", "segment", "fwd_status",
                                                    "oi_value_end") if name in columns}

    def lines():
        yield ",".join(columns) + "\n"
        for row in rows:
            if counts is not None:
                count_row(counts, row, where)
            yield ",".join(cell(value) for value in row) + "\n"

    return data_lake.write_csv_gz(fileobj, lines())


def count_row(counts: dict, row, where: dict) -> None:
    prefix = f"{row[where['symbol']]}|{row[where['tf']]}"
    key = f"{prefix}|{row[where['status']]}|{row[where['segment']]}"
    counts.setdefault("rows", {})[key] = counts.get("rows", {}).get(key, 0) + 1
    if "fwd_status" in where:
        fwd = f"{prefix}|{row[where['fwd_status']] or 'none'}"
        counts.setdefault("fwd_status", {})[fwd] = counts.get("fwd_status", {}).get(fwd, 0) + 1
    # Rows outside published mx months, or in a metrics gap; invalid candles carry no features and are not counted.
    if "oi_value_end" in where and row[where["oi_value_end"]] is None and row[where["status"]] != "invalid":
        counts.setdefault("oi_missing", {})[prefix] = counts.get("oi_missing", {}).get(prefix, 0) + 1


def file_name(symbol: str, minutes: int, through_month: str = LAST_MONTH) -> str:
    data_lake.validate_symbol(symbol)
    first = INTRADAY_FIRST_MONTH if minutes in INTRADAY_TIMEFRAMES else DAILY_FIRST_MONTH
    return f"candles__{symbol}__{minutes}__{first}_{check_through_month(through_month)}.csv.gz"


# ---------------------------------------------------------------- inputs and manifest


def check_through_month(month: str) -> str:
    data_lake.validate_month(month)
    if month > LAST_MONTH:
        raise CandleTableError(f"through_month must be at most {LAST_MONTH}: the hidden stretch is never read")
    if month < INTRADAY_FIRST_MONTH:
        raise CandleTableError(f"through_month must be at least {INTRADAY_FIRST_MONTH}")
    return month


def check_timeframes(timeframes) -> tuple:
    out = tuple(timeframes)
    if not out or any(type(m) is not int or m not in TABLE_TIMEFRAMES for m in out) or len(set(out)) != len(out):
        raise CandleTableError(f"timeframes must be distinct values of {TABLE_TIMEFRAMES}")
    return tuple(sorted(out))


def guard_months(through_month: str, token=None, gate=None) -> None:
    """The hidden guard over every month the build reads, before any file is opened."""
    require_months(data_lake.months_between(DAILY_WARMUP_MONTH, through_month), token, gate)
    check_through_month(through_month)


def load_daily_inputs(daily_dir, symbols, through_month: str = LAST_MONTH, *, token=None, gate=None):
    """(dk1 daily series per symbol incl. BTCUSDT, BTCUSDT funding) of 2020-01..through_month."""
    guard_months(through_month, token, gate)
    market = regime_v1.MARKET_SYMBOL
    wanted = [symbol for symbol in data_lake.SYMBOLS if symbol in set(symbols) | {market}]
    daily = {symbol: load_symbol_daily(daily_dir, symbol, DAILY_WARMUP_MONTH, through_month, token=token, gate=gate)
             for symbol in wanted}
    funding = load_symbol_funding_range(daily_dir, market, DAILY_WARMUP_MONTH, through_month, token=token,
                                        gate=gate)
    return daily, funding


def regime_join(daily: dict, btc_funding, through_month: str = LAST_MONTH) -> RegimeJoin:
    first_ms = data_lake.month_bounds_ms(regime_v1.FIRST_MONTH)[0]
    end_ms = data_lake.month_bounds_ms(check_through_month(through_month))[1]
    _, columns = regime_v1.build_table(daily, btc_funding, first_ms, end_ms)
    return RegimeJoin.from_build_table(columns, first_ms, end_ms)


def daily_contexts(symbol: str, daily, regime: RegimeJoin | None, timeframes=DAILY_TIMEFRAMES,
                   through_month: str = LAST_MONTH, *, profiles: dict | None = None, derivs=None) -> dict:
    days = candles_from_daily(daily, 1440)
    weeks = candles_from_daily(daily, 10080)
    sigma_days = daily_sigma(days)
    shared = loc.shared_levels(days=days, weeks=weeks)
    out = {}
    for minutes in timeframes:
        if minutes == 1440:
            candles, sigma = days, sigma_days
        else:
            candles = weeks
            sigma = weekly_sigma(days, candles, sigma_days)
        out[minutes] = TableContext(symbol, minutes, candles, sigma, DAILY_SIGMA_MODEL,
                                    table_segments(minutes, through_month), regime,
                                    levels=loc.context_levels(minutes, candles, shared),
                                    htf=loc.htf_sources(minutes, {}, days, weeks), history_days=days,
                                    profiles=profiles, derivs=derivs)
    return out


def intraday_context(symbol: str, minutes: int, bars: BarSeries, candles: CandleSeries, variance,
                     regime: RegimeJoin | None, through_month: str = LAST_MONTH, *, levels: dict | None = None,
                     htf: dict | None = None, history_days: CandleSeries | None = None,
                     profiles: dict | None = None, derivs=None) -> TableContext:
    """``levels``/``htf`` come from candle_location (shared_levels + context_levels, htf_sources)."""
    return TableContext(symbol, minutes, candles, intraday_sigma(bars, candles, variance), INTRADAY_SIGMA_MODEL,
                        table_segments(minutes, through_month), regime, levels=levels or {}, htf=htf or {},
                        history_days=history_days, profiles=profiles, derivs=derivs)


HASH_EXCLUDED = ("content_hash", "code_commit", "created_utc")


def build_manifest(*, files: list, counts: dict, inputs: dict, through_month: str, parity: dict,
                   symbols, timeframes, code_commit: str, created_utc: str,
                   registry: GroupRegistry = REGISTRY, funding_parity: dict | None = None) -> dict:
    """``content_hash`` covers everything except the commit and creation time, so an identical table
    built again maps to the same directory (which is then skipped)."""
    manifest = {"schema": SCHEMA, "group_versions": registry.versions(), "columns": registry.docs(),
                "symbols": list(symbols), "timeframes": list(timeframes), "through_month": through_month,
                "files": sorted(files, key=lambda item: item["name"]),
                "row_counts": dict(sorted(counts.get("rows", {}).items())),
                "fwd_status_counts": dict(sorted(counts.get("fwd_status", {}).items())),
                "oi_missing_counts": dict(sorted(counts.get("oi_missing", {}).items())),
                "oi_missing_counts_note": "per symbol|timeframe: non-invalid rows whose oi_value_end is empty "
                                          "(outside published mx months or in a metrics gap)",
                "funding_parity": funding_parity or {},
                "funding_source": "dk1 funding history for all rows; funding_parity compares it with the rd "
                                  "funding per symbol on their overlap",
                "inputs": inputs, "daily_parity": parity,
                "sigma_models": {"intraday": INTRADAY_SIGMA_MODEL, "daily": DAILY_SIGMA_MODEL},
                "code_commit": code_commit, "created_utc": created_utc,
                "notes": ["Descriptive table: no signal, nothing enters the experiment ledger.",
                          "*_prev columns read candles through i-1; outcomes read i+1..i+m inside the "
                          "decision's segment only; empty = unavailable."]}
    manifest["content_hash"] = content_hash({key: value for key, value in manifest.items()
                                             if key not in HASH_EXCLUDED})
    return manifest


def check_manifest(manifest: dict) -> None:
    if manifest.get("schema") != SCHEMA:
        raise ValueError("not a candle-table-v1 manifest")
    body = {key: value for key, value in manifest.items() if key not in HASH_EXCLUDED}
    if manifest.get("content_hash") != content_hash(body):
        raise ValueError("content_hash mismatch")


def report_dir(manifest: dict) -> str:
    check_manifest(manifest)
    return f"{REPORT_ROOT}/{DIR_PREFIX}__{manifest['content_hash'][:16]}"


def public_lines(manifest: dict) -> list:
    """Public log: counts only and the report hash."""
    check_manifest(manifest)
    by_status = {}
    for key, value in manifest["row_counts"].items():
        status = key.split("|")[2]
        by_status[status] = by_status.get(status, 0) + int(value)
    return [f"candle table {SCHEMA} files={len(manifest['files'])} "
            f"rows={sum(int(v) for v in manifest['row_counts'].values())} "
            + " ".join(f"{name}={by_status[name]}" for name in sorted(by_status)),
            f"report hash {manifest['content_hash']}"]


__all__ = ["SCHEMA", "REGISTRY", "Group", "GroupRegistry", "TableContext", "RegimeJoin", "build_rows", "write_table",
           "build_manifest", "check_manifest", "report_dir"]
