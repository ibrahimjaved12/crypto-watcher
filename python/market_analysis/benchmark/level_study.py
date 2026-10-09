"""Level-reaction event study L0 (#185): descriptive only. No trades, labels, exits or PASS/FAIL.

Unit: the label engine's point-in-time sigma, ``sigma_h(t) = horizon_sigma(variance[block], h) / 10**20``
(a fraction of price) with an EWMA half-life of 3 days, ``block`` = the last completed
5-minute block at decision minute ``t`` (``(t + 1) // 5 - 1``), ``h`` in {60, 240}.
Minutes without a published variance, or with a missing or compromised bar, take
part in nothing (no arming, no touch).

Event, per level ``L`` (``levels.py``) and approach side (support: price above ``L``;
resistance: price below ``L``), only at minutes in ``[valid_from, valid_to)``:

- away (arming) minute: support ``low >= L * (1 + sigma_240)``, resistance
  ``high <= L * (1 - sigma_240)`` (the minute extreme on the far side);
- touch: support ``low <= L * (1 + 0.1 * sigma_60)``, resistance
  ``high >= L * (1 - 0.1 * sigma_60)``;
- the event minute ``t0`` is the first touch after an away minute whose most recent
  away minute is at most 1440 minutes earlier; one event per arming, so chop around
  a level counts once and a new event needs a new away minute.

Search is per level with block min/max indexes over the four per-minute threshold
series (``u <= L`` / ``w >= L`` forms of the conditions), so the cost is about
events x block size, not minutes x levels.

Outcome from ``t0`` (sigma_60 fixed at ``t0``), 1-minute extremes over at most 1440
minutes starting with ``t0``: bounce target ``L (1 +/- 1 * sigma_60)`` on the approach
side, penetration barrier ``L (1 -/+ 0.25 * sigma_60)``. B bounce first, P penetration
first, T neither, A both in one minute (``t0`` included). Two extra exclusion statuses
(the spec's safest reading): X a missing or compromised minute before resolution,
I the window runs past the evaluated segment (data after the segment is never used).

Forward returns (simple, in basis points and in units of ``sigma_60 * sqrt(h / 60)``
at the decision minute), entry at the open of the minute after the decision
minute, exit at the open ``h`` minutes later: touch drift from ``t0`` (positive in the
bounce direction) for every event, penetration continuation from the penetration
minute (positive beyond the level) for P events. A window with a missing or
compromised minute, or past the segment end, is excluded and counted.

Cells: level set x side x symbol (and pooled) x tercile of sigma_240 at ``t0`` (and
all); BTC round tier cells (5000+, 10000) are compared with the whole placebo pool.
Tercile thresholds per symbol come from hourly sigma_240 samples of the development
segment only. Counts come first; rates (``B / (B + P)`` next to the Brownian
``z / (y + z) = 20 %``) and means are compared with placebo through a UTC-day cluster
bootstrap (B = 2000, day draws from ``rng.u64_words``, seed 20261009, one weight matrix
for every cell). Exploratory screening: no cell is a hypothesis test.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np

from .. import data_lake
from . import levels as lv
from .bars import COMPROMISED_FLAGS, MISSING, BarSeries
from .calibration import _render, check_segment
from .candles import build_candles
from .canonical import content_hash
from .market_data import load_symbol_bars
from .rng import u64_words
from .segments import segment_bounds_ms
from .volatility import BLOCK_MINUTES, VAR_SCALE, build_variance, horizon_sigma

SCHEMA = "level-study-v1"
SIGMA_HALF_LIFE_DAYS = 3
ARM_SIGMA_240 = 1.0
TOUCH_EPS_SIGMA_60 = 0.1
ARM_LOOKBACK_MIN = 1440
OUTCOME_WINDOW_MIN = 1440
BOUNCE_Y = 1.0
PENETRATION_Z = 0.25
BROWNIAN_BOUNCE_SHARE = PENETRATION_Z / (BOUNCE_Y + PENETRATION_Z)  # 0.2, continuous and driftless
RETURN_HORIZONS = (60, 120, 240, 1440, 2880, 7200)
BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 20261009
STATUSES = ("B", "P", "T", "A", "X", "I")
SIDES = ("support", "resistance")
TERCILES = ("low", "mid", "high")
BLOCK = 512
_MINUTE = data_lake.MINUTE_MS
_DAY = 1440 * _MINUTE
_INF = float("inf")


# ---------------------------------------------------------------- search index


class BlockIndex:
    """First/last index in [start, stop) with value <= or >= a threshold, via per-block min and max."""

    def __init__(self, values: np.ndarray):
        self.values = np.ascontiguousarray(values, dtype=float)
        starts = np.arange(0, max(len(self.values), 1), BLOCK)
        if len(self.values):
            self.mins = np.minimum.reduceat(self.values, starts)
            self.maxs = np.maximum.reduceat(self.values, starts)
        else:
            self.mins = self.maxs = np.empty(0)

    def _hit(self, segment, threshold, ge):
        return segment >= threshold if ge else segment <= threshold

    def first(self, threshold: float, start: int, stop: int, *, ge: bool):
        if start >= stop:
            return None
        v = self.values
        head_end = min((start // BLOCK + 1) * BLOCK, stop)
        hit = self._hit(v[start:head_end], threshold, ge)
        if hit.any():
            return start + int(np.argmax(hit))
        first_block, last_block = start // BLOCK + 1, (stop - 1) // BLOCK
        if first_block > last_block:
            return None
        aggregate = (self.maxs if ge else self.mins)[first_block:last_block + 1]
        candidates = self._hit(aggregate, threshold, ge)
        if not candidates.any():
            return None
        block = first_block + int(np.argmax(candidates))
        lo, hi = block * BLOCK, min((block + 1) * BLOCK, stop)
        hit = self._hit(v[lo:hi], threshold, ge)
        if hit.any():
            return lo + int(np.argmax(hit))
        return None if block == last_block else self.first(threshold, hi, stop, ge=ge)

    def last(self, threshold: float, start: int, stop: int, *, ge: bool):
        if start >= stop:
            return None
        v = self.values
        tail_start = max(((stop - 1) // BLOCK) * BLOCK, start)
        hit = self._hit(v[tail_start:stop], threshold, ge)
        if hit.any():
            return stop - 1 - int(np.argmax(hit[::-1]))
        first_block, last_block = start // BLOCK, (stop - 1) // BLOCK - 1
        if last_block < first_block:
            return None
        aggregate = (self.maxs if ge else self.mins)[first_block:last_block + 1]
        candidates = self._hit(aggregate, threshold, ge)
        if not candidates.any():
            return None
        block = last_block - int(np.argmax(candidates[::-1]))
        lo, hi = max(block * BLOCK, start), (block + 1) * BLOCK
        hit = self._hit(v[lo:hi], threshold, ge)
        if hit.any():
            return hi - 1 - int(np.argmax(hit[::-1]))
        return None if block == first_block else self.last(threshold, start, lo, ge=ge)


# ---------------------------------------------------------------- per-symbol context


@dataclass
class Context:
    bars: BarSeries
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    usable: np.ndarray          # bar present and not compromised
    s60: np.ndarray             # sigma_60 at decision minute t (fraction), NaN when unpublished
    s240: np.ndarray
    invalid_prefix: np.ndarray  # prefix count of unusable minutes
    variance: object
    indexes: dict


def _price(column) -> np.ndarray:
    raw = np.frombuffer(column, dtype=np.int64)
    return np.where(raw == MISSING, np.nan, raw.astype(float))


def build_context(bars: BarSeries) -> Context:
    variance = build_variance(bars, SIGMA_HALF_LIFE_DAYS).variance
    blocks = len(variance)
    s60_block, s240_block = np.full(blocks, np.nan), np.full(blocks, np.nan)
    for j, var in enumerate(variance):
        if var != MISSING and var > 0:
            s60_block[j] = horizon_sigma(var, 60) / VAR_SCALE
            s240_block[j] = horizon_sigma(var, 240) / VAR_SCALE
    minutes = bars.minutes
    block_of = (np.arange(minutes) + 1) // BLOCK_MINUTES - 1
    ok_block = (block_of >= 0) & (block_of < blocks)
    clipped = np.clip(block_of, 0, max(blocks - 1, 0))
    s60 = np.where(ok_block, s60_block[clipped] if blocks else np.nan, np.nan)
    s240 = np.where(ok_block, s240_block[clipped] if blocks else np.nan, np.nan)
    return make_context(bars, s60, s240, variance)


def make_context(bars: BarSeries, s60, s240, variance=None) -> Context:
    """Context from per-minute sigma arrays (build_context derives them from the EWMA; tests pass their own)."""
    s60, s240 = np.asarray(s60, dtype=float), np.asarray(s240, dtype=float)
    if variance is None:
        variance = []
    opens, high, low = _price(bars.open), _price(bars.high), _price(bars.low)
    flags = np.frombuffer(bars.flags, dtype=np.uint16)
    usable = ~np.isnan(opens) & ~np.isnan(high) & ~np.isnan(low) & ((flags & COMPROMISED_FLAGS) == 0)
    active = usable & ~np.isnan(s60) & ~np.isnan(s240)
    with np.errstate(invalid="ignore"):
        indexes = {
            ("support", "touch"): BlockIndex(np.where(active, low / (1 + TOUCH_EPS_SIGMA_60 * s60), _INF)),
            ("support", "away"): BlockIndex(np.where(active, low / (1 + ARM_SIGMA_240 * s240), -_INF)),
            ("resistance", "touch"): BlockIndex(np.where(active, high / (1 - TOUCH_EPS_SIGMA_60 * s60), -_INF)),
            ("resistance", "away"): BlockIndex(np.where(active, high / (1 - ARM_SIGMA_240 * s240), _INF)),
        }
    invalid_prefix = np.concatenate(([0], np.cumsum(~usable)))
    return Context(bars, opens, high, low, usable, s60, s240, invalid_prefix, variance, indexes)


def sigma240_int_at(ctx: Context, ms: int):
    """Integer horizon_sigma(var, 240) known at minute open time ``ms`` (decision = the minute before), or None."""
    index = (ms - ctx.bars.start_ms) // _MINUTE
    block = index // BLOCK_MINUTES - 1
    if not 0 <= block < len(ctx.variance):
        return None
    var = ctx.variance[block]
    return None if var == MISSING or var <= 0 else horizon_sigma(var, 240)


def _window(ctx: Context, level: lv.Level) -> tuple[int, int]:
    start = ctx.bars.start_ms
    first = max(0, -(-(level.valid_from_ms - start) // _MINUTE))
    stop = min(ctx.bars.minutes, max(0, -(-(level.valid_to_ms - start) // _MINUTE)))
    return first, stop


def detect_events(ctx: Context, level: lv.Level, side: str) -> list[int]:
    """Event minutes t0 of one level and side over its validity window (one event per arming)."""
    price = float(level.price)
    first, stop = _window(ctx, level)
    touch, away = ctx.indexes[(side, "touch")], ctx.indexes[(side, "away")]
    away_ge = side == "support"  # support: away when low/(1+s) >= L; resistance: away when high/(1-s) <= L
    touch_ge = side == "resistance"
    events, x = [], first
    while x < stop:
        a = away.first(price, x, stop, ge=away_ge)
        if a is None:
            break
        t = touch.first(price, a + 1, stop, ge=touch_ge)
        if t is None:
            break
        last_away = away.last(price, a, t, ge=away_ge)
        if t - last_away <= ARM_LOOKBACK_MIN:
            events.append(t)
        x = t + 1
    return events


def classify(ctx: Context, t0: int, price: int, side: str, end_index: int):
    """(status, penetration minute or None) of an event at t0; data at or after end_index is never read."""
    stop = t0 + OUTCOME_WINDOW_MIN
    if stop > end_index:
        return "I", None
    s = ctx.s60[t0]
    level = float(price)
    high, low, usable = ctx.high[t0:stop], ctx.low[t0:stop], ctx.usable[t0:stop]
    with np.errstate(invalid="ignore"):
        if side == "support":
            bounce, penetration = high >= level * (1 + BOUNCE_Y * s), low <= level * (1 - PENETRATION_Z * s)
        else:
            bounce, penetration = low <= level * (1 - BOUNCE_Y * s), high >= level * (1 + PENETRATION_Z * s)
    bounce &= usable
    penetration &= usable

    def first(mask):
        return int(np.argmax(mask)) if mask.any() else None

    b, p, bad = first(bounce), first(penetration), first(~usable)
    resolved = min(value for value in (b, p, OUTCOME_WINDOW_MIN) if value is not None)
    if bad is not None and bad <= resolved:
        return "X", None
    if b is None and p is None:
        return "T", None
    if b is not None and p is not None and b == p:
        return "A", None
    if p is None or (b is not None and b < p):
        return "B", None
    return "P", t0 + p


def forward_returns(ctx: Context, decision: int, sign: int, end_index: int) -> tuple[list, list]:
    """(bp, sigma units) per RETURN_HORIZONS; NaN when the window is unusable or past end_index."""
    entry = decision + 1
    s = ctx.s60[decision] if decision < len(ctx.s60) else np.nan
    bp, units = [], []
    for h in RETURN_HORIZONS:
        exit_ = entry + h
        if exit_ >= end_index or ctx.invalid_prefix[exit_ + 1] - ctx.invalid_prefix[entry] > 0 or not s > 0:
            bp.append(np.nan)
            units.append(np.nan)
            continue
        r = ctx.open[exit_] / ctx.open[entry] - 1
        bp.append(sign * 1e4 * r)
        units.append(sign * r / (s * sqrt(h / 60)))
    return bp, units


def tercile_thresholds(ctx: Context) -> list:
    """Per symbol, sigma_240 tercile cut points from hourly samples of the development segment only."""
    first_ms, end_ms = segment_bounds_ms("development")
    start = ctx.bars.start_ms
    lo = max(0, (first_ms - start) // _MINUTE)
    hi = min(ctx.bars.minutes, (end_ms - start) // _MINUTE)
    minutes = np.arange(lo, hi)
    hourly = minutes[((start // _MINUTE + minutes + 1) % 60) == 0]  # decision at the end of minute HH:59
    samples = ctx.s240[hourly]
    samples = samples[~np.isnan(samples)]
    if samples.size < 3:
        return [None, None]
    return [float(value) for value in np.quantile(samples, [1 / 3, 2 / 3])]


def _tercile(value: float, thresholds) -> int:
    if thresholds[0] is None or not value == value:
        return -1
    return 0 if value <= thresholds[0] else (1 if value <= thresholds[1] else 2)


# ---------------------------------------------------------------- level sets


def build_level_sets(ctx: Context, set_name: str, lo: int, hi: int) -> tuple[list, list]:
    """(real levels, placebo levels) of one set for one symbol."""
    symbol = ctx.bars.symbol
    if set_name == "round":
        real = lv.round_levels(symbol, lo, hi)
        placebo = [level for offset in lv.placebo_round_grids(symbol) for level in lv.round_levels(symbol, lo, hi,
                                                                                                    offset)]
        return real, placebo
    if set_name in ("prev_day", "prev_week"):
        real = lv.prev_period_levels(ctx.bars, "day" if set_name == "prev_day" else "week")
    elif set_name == "swing4h":
        real = lv.swing_levels(build_candles(ctx.bars, 240))
    else:
        raise ValueError(f"unknown level set {set_name!r}")
    groups = lv.placebo_offset_levels(real, [sigma240_int_at(ctx, level.valid_from_ms) for level in real])
    return real, [level for group in groups for level in group]


EVENT_FIELDS = ("day", "side", "placebo", "tier", "tercile", "status")


def _empty_events() -> dict:
    events = {name: [] for name in EVENT_FIELDS}
    for kind in ("touch_bp", "touch_sigma", "cont_bp", "cont_sigma"):
        events[kind] = []
    return events


def study_symbol(bars: BarSeries, segment: str, level_sets=lv.LEVEL_SETS, *, progress=None) -> dict:
    """Events of one symbol's bars (loaded from FIRST_MONTH to the segment end) for the given level sets."""
    first_ms, end_ms = segment_bounds_ms(segment)
    ctx = build_context(bars)
    start = bars.start_ms
    seg_first = max(0, (first_ms - start) // _MINUTE)
    seg_end = min(bars.minutes, (end_ms - start) // _MINUTE)
    thresholds = tercile_thresholds(ctx)
    usable_high, usable_low = ctx.high[ctx.usable], ctx.low[ctx.usable]
    lo, hi = (int(usable_low.min()), int(usable_high.max())) if usable_high.size else (1, 0)
    result = {"symbol": bars.symbol, "tercile_thresholds": thresholds, "levels": {}, "events": {}}
    for set_name in level_sets:
        real, placebo = build_level_sets(ctx, set_name, lo, hi)
        result["levels"][set_name] = {"real": len(real), "placebo": len(placebo)}
        events = _empty_events()
        for is_placebo, group in ((0, real), (1, placebo)):
            for level in group:
                for side_index, side in enumerate(SIDES):
                    for t0 in detect_events(ctx, level, side):
                        if not seg_first <= t0 < seg_end:
                            continue
                        status, penetration = classify(ctx, t0, level.price, side, seg_end)
                        touch_sign = 1 if side == "support" else -1
                        touch_bp, touch_sigma = forward_returns(ctx, t0, touch_sign, seg_end)
                        if penetration is not None:
                            cont_bp, cont_sigma = forward_returns(ctx, penetration, -touch_sign, seg_end)
                        else:
                            cont_bp = cont_sigma = [np.nan] * len(RETURN_HORIZONS)
                        values = ((start + t0 * _MINUTE - first_ms) // _DAY, side_index, is_placebo, level.tier,
                                  _tercile(ctx.s240[t0], thresholds), STATUSES.index(status))
                        for name, value in zip(EVENT_FIELDS, values):
                            events[name].append(value)
                        events["touch_bp"].append(touch_bp)
                        events["touch_sigma"].append(touch_sigma)
                        events["cont_bp"].append(cont_bp)
                        events["cont_sigma"].append(cont_sigma)
        result["events"][set_name] = _as_arrays(events)
        if progress is not None:
            progress()
    return result


def _as_arrays(events: dict) -> dict:
    out = {name: np.asarray(events[name], dtype=np.int64) for name in EVENT_FIELDS}
    for kind in ("touch_bp", "touch_sigma", "cont_bp", "cont_sigma"):
        out[kind] = np.asarray(events[kind], dtype=float).reshape(-1, len(RETURN_HORIZONS))
    return out


def run_symbol(bars_dir, symbol: str, segment: str, level_sets=lv.LEVEL_SETS, *, progress=None) -> dict:
    """Guarded: the hidden guard runs before any file is opened; bars FIRST_MONTH..segment end only."""
    months = check_segment(segment)
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    return study_symbol(bars, segment, level_sets, progress=progress)


# ---------------------------------------------------------------- bootstrap and cells


def bootstrap_weights(n_days: int, B: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED) -> np.ndarray:
    """(B, n_days) day multiplicities of a UTC-day cluster bootstrap; draws from rng.u64_words."""
    if n_days <= 0:
        return np.zeros((B, 0))
    words = u64_words(seed, "level-bootstrap", 0, B * n_days)
    draws = (words % np.uint64(n_days)).astype(np.int64).reshape(B, n_days)
    flat = (draws + np.arange(B)[:, None] * n_days).ravel()
    return np.bincount(flat, minlength=B * n_days).reshape(B, n_days).astype(float)


def bootstrap_difference(weights: np.ndarray, real_num, real_den, placebo_num, placebo_den) -> dict:
    """Ratio-of-sums real, placebo and real - placebo with a 95 % percentile interval (per-day sums in)."""
    columns = np.column_stack([real_num, real_den, placebo_num, placebo_den]).astype(float)
    totals = columns.sum(axis=0)
    drawn = weights @ columns if weights.size else np.zeros((0, 4))

    def ratio(num, den):
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)

    real = float(ratio(totals[0], totals[1]))
    placebo = float(ratio(totals[2], totals[3]))
    diffs = ratio(drawn[:, 0], drawn[:, 1]) - ratio(drawn[:, 2], drawn[:, 3])
    defined = diffs[~np.isnan(diffs)]
    lo, hi = (float(v) for v in np.percentile(defined, [2.5, 97.5])) if defined.size else (float("nan"),) * 2
    return {"real": real, "placebo": placebo, "difference": real - placebo, "ci95": [lo, hi],
            "real_n": float(totals[1]), "placebo_n": float(totals[3]),
            "undefined_draws": int(diffs.size - defined.size)}


def _day_sums(days, values, n_days) -> np.ndarray:
    return np.bincount(days, weights=values, minlength=n_days)[:n_days] if days.size else np.zeros(n_days)


def cell_summary(real: dict, placebo: dict, n_days: int, weights: np.ndarray) -> dict:
    """Counts first, then bounce share and mean returns versus placebo for one cell (event-array subsets)."""
    def counts(events):
        return {status: int((events["status"] == i).sum()) for i, status in enumerate(STATUSES)}

    def per_day(events, mask):
        return _day_sums(events["day"][mask], np.ones(int(mask.sum())), n_days)

    out = {"counts": {"real": counts(real), "placebo": counts(placebo)}}
    b, p = STATUSES.index("B"), STATUSES.index("P")
    bounce = bootstrap_difference(weights, per_day(real, real["status"] == b),
                                  per_day(real, (real["status"] == b) | (real["status"] == p)),
                                  per_day(placebo, placebo["status"] == b),
                                  per_day(placebo, (placebo["status"] == b) | (placebo["status"] == p)))
    out["bounce_share"] = {**bounce, "brownian_reference": BROWNIAN_BOUNCE_SHARE}
    for kind, eligible in (("touch", None), ("cont", p)):
        block, excluded = {}, {}
        for unit in ("bp", "sigma"):
            for index, h in enumerate(RETURN_HORIZONS):
                sums = []
                for events in (real, placebo):
                    values = events[f"{kind}_{unit}"][:, index] if len(events["day"]) else np.empty(0)
                    applies = np.ones(len(events["day"]), bool) if eligible is None else events["status"] == eligible
                    ok = applies & ~np.isnan(values)
                    sums.append((_day_sums(events["day"][ok], values[ok], n_days),
                                 per_day(events, ok)))
                    if unit == "bp":
                        excluded.setdefault(str(h), []).append(int((applies & np.isnan(values)).sum()))
                block[f"{unit}_{h}"] = bootstrap_difference(weights, sums[0][0], sums[0][1], sums[1][0], sums[1][1])
        out[f"{kind}_returns"] = block
        out[f"{kind}_excluded"] = {h: {"real": pair[0], "placebo": pair[1]} for h, pair in excluded.items()}
    return out


def _subset(events: dict, mask) -> dict:
    return {name: values[mask] for name, values in events.items()}


def build_cells(results: dict, level_sets, n_days: int, weights: np.ndarray) -> list[dict]:
    """Every cell: set x side x symbol (and pooled) x tercile (and all); BTC round tier cells."""
    cells = []
    symbols = list(results)
    for set_name in level_sets:
        tables = {symbol: results[symbol]["events"][set_name] for symbol in symbols}
        pooled = {name: np.concatenate([tables[s][name] for s in symbols]) for name in tables[symbols[0]]}
        for side_index, side in enumerate(SIDES):
            for symbol, table in [*tables.items(), ("pooled", pooled)]:
                for tercile_index, tercile in [(-2, "all"), *enumerate(TERCILES)]:
                    mask = table["side"] == side_index
                    if tercile_index >= 0:
                        mask &= table["tercile"] == tercile_index
                    real = _subset(table, mask & (table["placebo"] == 0))
                    placebo = _subset(table, mask & (table["placebo"] == 1))
                    cells.append({"set": set_name, "side": side, "symbol": symbol, "tercile": tercile, "tier": "all",
                                  **cell_summary(real, placebo, n_days, weights)})
            if set_name == "round" and "BTCUSDT" in tables:
                table = tables["BTCUSDT"]
                side_mask = table["side"] == side_index
                placebo = _subset(table, side_mask & (table["placebo"] == 1))
                for tier_name, minimum in (("5000+", 5000), ("10000", 10000)):
                    real = _subset(table, side_mask & (table["placebo"] == 0) & (table["tier"] >= minimum))
                    cells.append({"set": set_name, "side": side, "symbol": "BTCUSDT", "tercile": "all",
                                  "tier": tier_name, **cell_summary(real, placebo, n_days, weights)})
    return cells


# ---------------------------------------------------------------- report


def build_report(segment: str, results: dict, level_sets, *, code_commit: str, created_utc: str,
                 B: int = BOOTSTRAP_B) -> dict:
    first_ms, end_ms = segment_bounds_ms(segment)
    n_days = (end_ms - first_ms) // _DAY
    weights = bootstrap_weights(n_days, B)
    cells = build_cells(results, level_sets, n_days, weights)
    symbols = {}
    for symbol, result in results.items():
        symbols[symbol] = {"tercile_thresholds_sigma240": result["tercile_thresholds"], "levels": result["levels"],
                           "events": {name: {"real": int((table["placebo"] == 0).sum()),
                                             "placebo": int((table["placebo"] == 1).sum())}
                                      for name, table in result["events"].items()}}
    report = {
        "schema": SCHEMA, "segment": segment, "level_sets": list(level_sets), "symbols": _render(symbols),
        "parameters": _render({"sigma_half_life_days": SIGMA_HALF_LIFE_DAYS, "arm_sigma_240": ARM_SIGMA_240,
                               "touch_eps_sigma_60": TOUCH_EPS_SIGMA_60, "arm_lookback_min": ARM_LOOKBACK_MIN,
                               "outcome_window_min": OUTCOME_WINDOW_MIN, "bounce_y": BOUNCE_Y,
                               "penetration_z": PENETRATION_Z, "return_horizons_min": list(RETURN_HORIZONS),
                               "bootstrap_B": B, "bootstrap_seed": BOOTSTRAP_SEED,
                               "placebo_count": lv.PLACEBO_COUNT, "placebo_seed": lv.PLACEBO_SEED,
                               "brownian_bounce_share": BROWNIAN_BOUNCE_SHARE}),
        "n_cells": len(cells), "n_days": n_days,
        "notes": ["Exploratory screening: no cell is a hypothesis test by itself; the number of cells is the "
                  "multiplicity.",
                  "BTC round tier cells (5000+, 10000) are compared with the whole BTC round placebo pool.",
                  "Statuses X (missing or compromised minute before resolution) and I (window past the segment) "
                  "are exclusions, never resolved.",
                  "Round placebo stream: level-placebo:<SYMBOL>:round (rng streams cannot contain '|')."],
        "cells": _render(cells), "code_commit": code_commit, "created_utc": created_utc}
    report["report_hash"] = content_hash(report)
    return report


def check_report(report: dict) -> None:
    body = {key: value for key, value in report.items() if key != "report_hash"}
    if report.get("report_hash") != content_hash(body):
        raise ValueError("report_hash mismatch")


def report_paths(report: dict) -> tuple[str, str]:
    stem = f"reports/levels/{report['segment']}__{report['report_hash'][:16]}"
    return stem + ".json", stem + ".md"


def public_lines(report: dict) -> list[str]:
    """Public log: per symbol x level set only symbol, set, segment, real and placebo event counts; the hash."""
    check_report(report)
    lines = [f"level study segment {report['segment']}"]
    for symbol, result in report["symbols"].items():
        for set_name, counts in result["events"].items():
            lines.append(f"{symbol} set={set_name} segment={report['segment']} events={int(counts['real'])} "
                         f"placebo_events={int(counts['placebo'])}")
    lines.append(f"report hash {report['report_hash']}")
    return lines


def _cell(value) -> str:
    return "n/a" if value is None else str(value)


def markdown(report: dict) -> str:
    check_report(report)
    lines = [f"# Level-reaction event study L0 ({report['segment']})", "",
             f"Report hash: `{report['report_hash']}`. Code commit `{report['code_commit']}`, "
             f"created {report['created_utc']}. Cells: {report['n_cells']} (exploratory; the cell count is the "
             f"multiplicity). Brownian bounce share reference: {report['parameters']['brownian_bounce_share']}.", ""]
    lines += [f"- {note}" for note in report["notes"]] + [""]
    lines += ["## Events", "", "| symbol | set | levels | placebo levels | events | placebo events |",
              "| --- | --- | --- | --- | --- | --- |"]
    for symbol, result in report["symbols"].items():
        for set_name, counts in result["events"].items():
            levels = result["levels"][set_name]
            lines.append(f"| {symbol} | {set_name} | {levels['real']} | {levels['placebo']} | {counts['real']} | "
                         f"{counts['placebo']} |")
    lines += ["", "## Cells (counts first)", "",
              "| set | side | symbol | tercile | tier | real B/P/T/A/X/I | placebo B/P/T/A/X/I | B/(B+P) real | "
              "placebo | diff | 95% CI |", "|" + " --- |" * 11]
    for cell in report["cells"]:
        real = "/".join(str(cell["counts"]["real"][s]) for s in STATUSES)
        placebo = "/".join(str(cell["counts"]["placebo"][s]) for s in STATUSES)
        share = cell["bounce_share"]
        lines.append(f"| {cell['set']} | {cell['side']} | {cell['symbol']} | {cell['tercile']} | {cell['tier']} | "
                     f"{real} | {placebo} | {_cell(share['real'])} | {_cell(share['placebo'])} | "
                     f"{_cell(share['difference'])} | {_cell(share['ci95'][0])} .. {_cell(share['ci95'][1])} |")
    lines += ["", "Forward-return means, exclusions and their intervals per cell are in the JSON report.", ""]
    return "\n".join(lines) + "\n"
