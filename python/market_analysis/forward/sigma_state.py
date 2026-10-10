"""Incremental labels-v3 robust/hcal state. No I/O, clock, or rolling c_h approximation.

Wire integers are decimal strings inside payload (JS must never round the EWMA). Ratios
are lossless rational encodings of the reference float, in HCAL_SCALE units; quantizing
individual ratios to ppm would change the reference median. Only c_h is rounded to ppm.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from fractions import Fraction
from math import log

from ..benchmark import volatility as v
from ..benchmark.bars import BarSeries, COMPROMISED_FLAGS, MISSING
from ..benchmark.canonical import content_hash
from .setups import FORWARD_PARAMS

SIGMA_VERSION = "forward-sigma-v1"
MINUTE_MS = 60_000
POINT_HISTORY_MS = 2 * v.DAY_MS


def _wire(value):
    if type(value) is int:
        return str(value)
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _unwire(value):
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    if isinstance(value, dict):
        return {key: _unwire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwire(item) for item in value]
    return value


@dataclass
class SigmaState:
    symbol: str
    start_ms: int
    as_of_ms: int
    params_hash: str
    grids: list  # [horizon, half_life, step]
    ewma: dict = field(default_factory=dict)  # half-life -> A, W, n, published
    day: int | None = None
    day_blocks: list = field(default_factory=list)
    seasonal_days: list = field(default_factory=list)  # [day, slot sums, counts]; qualified only
    factors: list | None = None
    previous_close: int | None = None
    calibration: dict = field(default_factory=dict)  # horizon -> ratios, first_counted_ms
    pending: list = field(default_factory=list)  # exit, entry, horizon, open, robust sigma
    points: dict = field(default_factory=dict)  # bounded retry cache; horizon -> timestamp -> [level,c,sigma]

    def to_record(self):
        payload = _wire(asdict(self))
        body = {"symbol": self.symbol, "sigma_version": SIGMA_VERSION,
                "as_of_ms": self.as_of_ms, "payload": payload}
        return {**body, "checksum": content_hash(body)}

    @classmethod
    def from_record(cls, record, *, symbol=None, params=FORWARD_PARAMS):
        if not isinstance(record, dict) or record.get("sigma_version") != SIGMA_VERSION:
            raise ValueError("sigma state version mismatch")
        body = {key: record.get(key) for key in ("symbol", "sigma_version", "as_of_ms", "payload")}
        if content_hash(body) != record.get("checksum"):
            raise ValueError("sigma state checksum mismatch")
        try:
            state = cls(**_unwire(record["payload"]))
        except (TypeError, KeyError):
            raise ValueError("invalid sigma state payload") from None
        if (state.symbol != record["symbol"] or (symbol is not None and state.symbol != symbol)
                or state.as_of_ms != record["as_of_ms"] or state.params_hash != params.identity()
                or state.grids != [[h, params.half_life(h), params.step(h)] for h in params.horizons]):
            raise ValueError("sigma state identity mismatch")
        return state


def empty_state(symbol, start_ms, params=FORWARD_PARAMS):
    if start_ms % v.BLOCK_MS:
        raise ValueError("sigma history must start on a five-minute boundary")
    grids = [[h, params.half_life(h), params.step(h)] for h in params.horizons]
    return SigmaState(symbol, start_ms, start_ms - MINUTE_MS, params.identity(), grids,
                      ewma={str(d): [0, 0, 0, MISSING] for _, d, _ in grids},
                      calibration={str(h): {"ratios": [], "first_counted_ms": None} for h, _, _ in grids},
                      points={str(h): {} for h, _, _ in grids})


def _day(state, day):
    if state.day == day:
        return
    if state.day is not None:
        blocks = state.day_blocks
        total = sum(value for _, value in blocks)
        if len(blocks) >= v.VALID_DAY_BLOCKS and total > 0:
            sums, counts = [0] * v.SLOTS_PER_DAY, [0] * v.SLOTS_PER_DAY
            for slot, value in blocks:
                sums[slot] += value * len(blocks) * v._U_SCALE // total
                counts[slot] += 1
            state.seasonal_days.append([state.day, sums, counts])
    state.day, state.day_blocks = day, []
    state.seasonal_days = [item for item in state.seasonal_days if day - v.SEASONAL_WINDOW_DAYS <= item[0] < day]
    state.factors = None
    if len(state.seasonal_days) >= v.SEASONAL_MIN_DAYS:
        sums = [sum(item[1][s] for item in state.seasonal_days) for s in range(v.SLOTS_PER_DAY)]
        counts = [sum(item[2][s] for item in state.seasonal_days) for s in range(v.SLOTS_PER_DAY)]
        state.factors = v._normalise(sums, counts)
    for h in state.points:
        state.points[h] = {t: point for t, point in state.points[h].items()
                           if int(t) >= day * v.DAY_MS - POINT_HISTORY_MS}


def advance(state: SigmaState, bars: BarSeries, *, cutoff_ms=None, on_point=None) -> SigmaState:
    """Mutate state through available minutes < cutoff_ms; overlap is ignored, gaps are missing.

    as_of_ms is the open timestamp of the last consumed minute. The entry open at t
    completes calibration windows at t before sigma(t) is published; the close at t
    can only affect subsequent decisions. Pending windows survive chunk boundaries.
    """
    if bars.symbol != state.symbol:
        raise ValueError("sigma bars belong to another symbol")
    end = min(bars.end_ms, cutoff_ms) if cutoff_ms is not None else bars.end_ms
    if end % MINUTE_MS:
        raise ValueError("sigma cutoff must be minute aligned")
    if end - MINUTE_MS <= state.as_of_ms:
        return state
    medians = {}
    for h, calibration in state.calibration.items():
        median = v._RunningMedian()
        for numerator, denominator in calibration["ratios"]:
            median.add(numerator / (denominator * v.HCAL_SCALE))
        medians[h] = median
    pending = {item[0]: [] for item in state.pending}
    for item in state.pending:
        pending[item[0]].append(item)
    for ms in range(state.as_of_ms + MINUTE_MS, end, MINUTE_MS):
        _day(state, ms // v.DAY_MS)
        i = (ms - bars.start_ms) // MINUTE_MS
        valid = 0 <= i < bars.minutes and not bars.flags[i] & COMPROMISED_FLAGS
        opened = bars.open[i] if valid else MISSING
        close = bars.close[i] if valid else MISSING
        for _, entry, h, price, base in pending.pop(ms, []):
            if opened != MISSING:
                ratio = abs(log(opened / price)) / (base / v.VAR_SCALE)
                exact = Fraction.from_float(ratio) * v.HCAL_SCALE
                calibration = state.calibration[str(h)]
                calibration["ratios"].append([exact.numerator, exact.denominator])
                medians[str(h)].add(ratio)
                if calibration["first_counted_ms"] is None:
                    calibration["first_counted_ms"] = entry
        for h, half_life, step in state.grids:
            if ms <= state.start_ms or ms % (step * MINUTE_MS):
                continue
            level = state.ewma[str(half_life)][3]
            base = None if level == MISSING or state.factors is None else v.horizon_sigma_robust(
                level, state.factors, (ms % v.DAY_MS) // v.BLOCK_MS, h)
            calibration = state.calibration[str(h)]
            first = calibration["first_counted_ms"]
            multiplier = None
            if first is not None and ms - first >= v.HCAL_MIN_DAYS * v.DAY_MS:
                multiplier = round(min(max(medians[str(h)].median() / v.HCAL_MEDIAN_ABS_NORMAL,
                                           v.HCAL_CLIP[0]), v.HCAL_CLIP[1]) * v.HCAL_SCALE)
            point = [level, multiplier, None if base is None or multiplier is None else base * multiplier // v.HCAL_SCALE]
            state.points[str(h)][str(ms)] = point
            if on_point is not None:
                on_point(h, ms, point)
            if base and opened != MISSING:
                exit_ms = ms + h * MINUTE_MS
                pending.setdefault(exit_ms, []).append([exit_ms, ms, h, opened, base])
        if (ms + MINUTE_MS) % v.BLOCK_MS == 0:
            previous = state.previous_close
            value = abs(close - previous) * v.ABS_SCALE // previous if close != MISSING and previous is not None else None
            state.previous_close = close if close != MISSING else None
            if value is not None:
                slot = (ms % v.DAY_MS) // (v.BLOCK_MS * v.BLOCKS_PER_SLOT)
                state.day_blocks.append([slot, value])
                if state.factors is not None:
                    adjusted = value * v.FACTOR_SCALE // state.factors[slot]
                    for key, (A, W, n, published) in state.ewma.items():
                        A = ((v.LAMBDA_NUM[int(key)] * A) >> v.LAMBDA_SHIFT) + adjusted
                        W = ((v.LAMBDA_NUM[int(key)] * W) >> v.LAMBDA_SHIFT) + (1 << v.LAMBDA_SHIFT)
                        n += 1
                        scale = ((A << v.LAMBDA_SHIFT) // W) * 1_000_000 // v.MEAN_ABS_NORMAL_PPM
                        if scale > v._INT64_MAX:
                            raise OverflowError("robust scale exceeds int64")
                        state.ewma[key] = [A, W, n, scale if n >= v.BLOCKS_PER_DAY * int(key) else MISSING]
        state.as_of_ms = ms
    state.pending = sorted(item for items in pending.values() for item in items)
    return state


def sigma_at(state: SigmaState, t: int, horizon=15):
    """Exact sigma at a retained decision; no invented value for an expired/future query."""
    if t > state.as_of_ms or t < state.day * v.DAY_MS - POINT_HISTORY_MS:
        raise ValueError("sigma decision outside retained state history")
    point = state.points.get(str(horizon), {}).get(str(t))
    return None if point is None else point[2]


def seed_state(history_bars: BarSeries, cutoff_ms: int, params=FORWARD_PARAMS):
    if not history_bars.start_ms < cutoff_ms <= history_bars.end_ms:
        raise ValueError("seed cutoff outside history")
    return advance(empty_state(history_bars.symbol, history_bars.start_ms, params), history_bars, cutoff_ms=cutoff_ms)


class StateSigma:
    """ForwardSigma's setup-facing protocol, retaining this call's decisions transiently."""
    def __init__(self, state, bars, params=FORWARD_PARAMS):
        self.params, self.robust = params, self
        self.points = {h: dict(points) for h, points in state.points.items()}
        advance(state, bars, on_point=lambda h, t, point: self.points[str(h)].__setitem__(str(t), point))

    def robust_point(self, horizon, signal_ms):
        point = self.points.get(str(horizon), {}).get(str(signal_ms))
        return None if point is None or point[2] is None else tuple(point)
