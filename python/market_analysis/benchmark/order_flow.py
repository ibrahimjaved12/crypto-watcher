"""Order-flow (taker imbalance) signal family ``of-v1`` for the #182 benchmark (slice H2).

Inputs are the integer BarSeries columns ``volume`` and ``taker_buy_volume``. The
imbalance of a set of minutes S is ``(2 * sum(TB) - sum(V)) / sum(V)`` as an exact
Fraction, undefined when ``sum(V) == 0``. Everything is integers and Fractions; no
float is used.

Signals are on the labels-v1 grid of the 240-minute horizon (15 minutes since slice
H1): ``signal_ms`` is the decision time, minute ``d = signal_ms / 60_000 - 1`` (as an
index into the bars) is the LAST minute observed, and nothing from minute ``d + 1``
or later is read. Entry is the next minute's open (labels-v1).

Robust z (both strategies): over the strategy's history values (the same quantity
at earlier decision times inside ``[t - history_days, t)``, valid values only),
``z = (x - median) / (14826/10000 * MAD)`` with ``MAD = median(|v - median|)``; the
median of an even count is the mean of the two middle values. ``z`` is defined only
with at least ``min_history`` history values and ``MAD > 0``. ``side = sign(z)``
(continuation).

- ``of_toh1m_4h``: ``x_H`` is the imbalance of the single minute opening at HH:00
  UTC, decided at ``signal_ms = HH:15``. A minute is valid when it has no
  ``COMPROMISED_FLAGS`` bit and ``V > 0``. An hour whose HH:00 open is within one
  minute of a funding ``calc_time_ms`` (``|calc - H| <= 60_000``) is excluded
  entirely: no signal and no history value (a different flow regime).
  Signal: ``|z| >= 3/2``. Watch item (intended): while a contract settles hourly
  (Binance switches to 1-hour funding after a rate-cap hit) every hour is a
  settlement hour, so all those hours drop out and the sampling becomes irregular;
  the count mode of the experiment runner shows the resulting signal rate.
- ``of_cum240_4h``: ``I_t`` is the imbalance of the 240 minutes ending at minute
  ``d`` (inclusive), at hourly decision times only (``signal_ms % 60 min == 0``); it
  is invalid if any of those minutes is compromised or ``sum(V) == 0``. Signal on a
  crossing: ``|z_t| >= 2`` while ``|z_prev| < 2`` at the previous hourly decision
  (``z_prev`` undefined is no crossing). After a signal the strategy is disarmed
  until ``|z| < 1``.

Gaps follow ta_strategies' rule (a crossing needs both the previous and current
value published; indicator state restarts after an invalid value): no value is
computed over a window holding a compromised minute, invalid values never enter the
history, and an undefined ``z`` (invalid value or too little history) restarts the
cum240 crossing (``z_prev`` becomes undefined, so the next defined value cannot
cross). The armed flag is strategy state, not indicator state: it is re-set only by
``|z| < 1``, never by a gap. History windows are not recursive, so nothing else is
carried across a gap. Cooldown (both): no signal less
than 240 minutes after the previous signal of the same symbol and strategy;
suppressed crossings change no state. Each symbol is processed independently and
deterministically.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right, insort
from collections import deque
from fractions import Fraction

from .. import data_lake
from .bars import COMPROMISED_FLAGS, BarSeries, CompromisedIndex
from .canonical import exact_to_str
from .funding import FundingSeries
from .runner import StrategySpec

VERSION = "of-v1"
HORIZON = 240
K_VALUES = ["2"]
RR_INDICES = [1]  # rr_grid[1] = 3/2
MAD_SCALE = Fraction(14826, 10000)
_MINUTE = data_lake.MINUTE_MS
_HOUR = 60 * _MINUTE
_DAY = 1440 * _MINUTE


def imbalance(volume: int, taker_buy_volume: int) -> Fraction | None:
    """(2 TB - V) / V for summed integer volumes; None when V == 0."""
    if volume <= 0:
        return None
    return Fraction(2 * taker_buy_volume - volume, volume)


def _kth_two(a, la, b, lb, k):
    """k-th smallest (0-based) of the union of two ascending sequences given as accessors."""
    lo, hi = max(0, k + 1 - lb), min(k + 1, la)
    while lo < hi:  # i elements from a, k + 1 - i from b
        i = (lo + hi) // 2
        if a(i) < b(k - i):
            lo = i + 1
        else:
            hi = i
    i, j = lo, k + 1 - lo
    candidates = ([a(i - 1)] if i else []) + ([b(j - 1)] if j else [])
    return max(candidates)


def median_mad(sorted_values: list) -> tuple[Fraction, Fraction]:
    """Exact (median, MAD) of a non-empty ascending list in O(log n) comparisons."""
    s, n = sorted_values, len(sorted_values)
    if not n:
        raise ValueError("median of an empty list")
    middle = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    q = bisect_right(s, middle)  # s[:q] <= median < s[q:]: deviations ascend away from the median
    lower = lambda i: middle - s[q - 1 - i]  # noqa: E731
    upper = lambda j: s[q + j] - middle  # noqa: E731
    if n % 2:
        mad = _kth_two(lower, q, upper, n - q, n // 2)
    else:
        mad = (_kth_two(lower, q, upper, n - q, n // 2 - 1) + _kth_two(lower, q, upper, n - q, n // 2)) / 2
    return Fraction(middle), Fraction(mad)


def robust_z(sorted_values: list, x: Fraction, min_history: int) -> Fraction | None:
    """(x - median) / (14826/10000 * MAD), or None with too little history or MAD == 0."""
    if len(sorted_values) < min_history:
        return None
    middle, mad = median_mad(sorted_values)
    if mad == 0:
        return None
    return (x - middle) / (MAD_SCALE * mad)


class TrailingHistory:
    """Valid values at earlier decision times inside a trailing [t - span, t) window."""

    def __init__(self, span_ms: int):
        self.span_ms, self.items, self.sorted = span_ms, deque(), []

    def at(self, time_ms: int) -> list:
        while self.items and self.items[0][0] < time_ms - self.span_ms:
            _, value = self.items.popleft()
            del self.sorted[bisect_left(self.sorted, value)]
        return self.sorted

    def add(self, time_ms: int, value: Fraction) -> None:
        self.items.append((time_ms, value))
        insort(self.sorted, value)


def sign(value: Fraction) -> int:
    return 1 if value > 0 else -1


def crossing_signals(zs, *, theta: Fraction, rearm_below: Fraction, cooldown_ms: int) -> list:
    """[(signal_ms, z)] where |z| crosses theta from below while armed, outside the cooldown.

    ``zs`` yields (signal_ms, z or None) in time order. An undefined z restarts the
    crossing (the previous value becomes undefined). A signal disarms until
    ``|z| < rearm_below``; the armed flag is never reset by a gap. A crossing inside
    the cooldown changes no state.
    """
    signals, last, armed, z_previous = [], None, True, None
    for signal_ms, z in zs:
        if z is None:
            z_previous = None
            continue
        if not armed and abs(z) < rearm_below:
            armed = True
        crossing = z_previous is not None and abs(z_previous) < theta <= abs(z)
        if armed and crossing and (last is None or signal_ms - last >= cooldown_ms):
            signals.append((signal_ms, z))
            armed, last = False, signal_ms
        z_previous = z
    return signals


def _first_hour_index(bars: BarSeries) -> int:
    return -bars.start_ms % _HOUR // _MINUTE


def _funding_near(calc_times: tuple, ms: int, guard_ms: int) -> bool:
    index = bisect_left(calc_times, ms - guard_ms)
    return index < len(calc_times) and calc_times[index] <= ms + guard_ms


def toh1m(bars: BarSeries, funding: FundingSeries, *, theta: Fraction, decision_offset_min: int,
          history_days: int, min_history: int, cooldown_min: int, funding_guard_min: int) -> list:
    """[(signal_ms, side)] for the top-of-hour 1-minute imbalance; signal at HH:00 + offset."""
    history, signals, last = TrailingHistory(history_days * _DAY), [], None
    calc_times = funding.calc_time_ms
    for i in range(_first_hour_index(bars), bars.minutes, 60):
        hour_ms = bars.open_time(i)
        signal_ms = hour_ms + decision_offset_min * _MINUTE
        if i + decision_offset_min - 1 >= bars.minutes:  # minute d is not in the data
            break
        if _funding_near(calc_times, hour_ms, funding_guard_min * _MINUTE):
            continue
        x = None if bars.flags[i] & COMPROMISED_FLAGS else imbalance(bars.volume[i], bars.taker_buy_volume[i])
        if x is None:
            continue
        z = robust_z(history.at(hour_ms), x, min_history)
        history.add(hour_ms, x)
        if z is not None and abs(z) >= theta and (last is None or signal_ms - last >= cooldown_min * _MINUTE):
            signals.append((signal_ms, sign(z)))
            last = signal_ms
    return signals


def cum240_z_series(bars: BarSeries, *, window_min: int, decision_step_min: int, history_days: int,
                    min_history: int) -> list:
    """[(signal_ms, z or None)] of the rolling-window imbalance at every decision time (the cum240 z series)."""
    compromised = CompromisedIndex(bars)
    history, zs = TrailingHistory(history_days * _DAY), []
    step = decision_step_min * _MINUTE
    first = -bars.start_ms % step // _MINUTE  # index of the first decision-time minute
    for e in range(first, bars.minutes + 1, decision_step_min):  # e = d + 1: signal_ms is bars.open_time(e)
        signal_ms = bars.start_ms + e * _MINUTE
        d = e - 1
        value = None
        if d - window_min + 1 >= 0 and not compromised.any_in(d - window_min + 1, d):
            value = imbalance(sum(bars.volume[d - window_min + 1:d + 1]),
                              sum(bars.taker_buy_volume[d - window_min + 1:d + 1]))
        z = None if value is None else robust_z(history.at(signal_ms), value, min_history)
        if value is not None:
            history.add(signal_ms, value)
        zs.append((signal_ms, z))  # None restarts the crossing, as indicators do after an invalid candle
    return zs


def cum240(bars: BarSeries, funding: FundingSeries, *, theta: Fraction, rearm_below: Fraction, window_min: int,
           decision_step_min: int, history_days: int, min_history: int, cooldown_min: int) -> list:
    """[(signal_ms, side)] for crossings of the rolling-window imbalance z (re-armed below rearm_below)."""
    del funding  # same builder signature as toh1m; the cumulative window ignores funding hours
    zs = cum240_z_series(bars, window_min=window_min, decision_step_min=decision_step_min,
                         history_days=history_days, min_history=min_history)
    return [(signal_ms, sign(z)) for signal_ms, z in crossing_signals(
        zs, theta=theta, rearm_below=rearm_below, cooldown_ms=cooldown_min * _MINUTE)]


_COMMON = {"history_days": 30, "min_history": 500, "cooldown_min": 240}
# name -> (kind, parameters, rule)
STRATEGIES = {
    "of_toh1m_4h": ("toh1m", {"theta": Fraction(3, 2), "decision_offset_min": 15, "funding_guard_min": 1,
                              **_COMMON}, toh1m),
    "of_cum240_4h": ("cum240", {"theta": Fraction(2), "rearm_below": Fraction(1), "window_min": 240,
                                "decision_step_min": 60, **_COMMON}, cum240),
}


def strategy_config(name: str, minutes: int) -> dict:
    """Every parameter, with Fractions as exact text, plus the fixed label geometry."""
    kind, parameters, _ = STRATEGIES[name]
    values = {key: exact_to_str(value) if isinstance(value, Fraction) else value for key, value in parameters.items()}
    return {"kind": kind, **values, "mad_scale": exact_to_str(MAD_SCALE), "timeframe_min": minutes,
            "k_values": list(K_VALUES), "rr_indices": list(RR_INDICES)}


def symbol_signals(name: str, bars: BarSeries, funding: FundingSeries, minutes: int, *, first_ms: int,
                   end_ms: int, label_step_min: int) -> list:
    """(symbol, signal_ms, side, minutes) of one symbol for signals in [first_ms, end_ms)."""
    if name not in STRATEGIES:
        raise ValueError(f"unknown order-flow strategy {name!r}")
    if minutes != HORIZON:
        raise ValueError(f"order-flow strategies are {HORIZON}-minute only")
    if type(first_ms) is not int or type(end_ms) is not int or end_ms < first_ms:
        raise ValueError("signal window must be integer [first_ms, end_ms)")
    _, parameters, rule = STRATEGIES[name]
    rows = []
    for signal_ms, side in rule(bars, funding, **parameters):
        if first_ms <= signal_ms < end_ms:
            if signal_ms % (label_step_min * _MINUTE):
                raise ValueError(f"{bars.symbol} signal {signal_ms} is off the {minutes}m label grid")
            rows.append((bars.symbol, signal_ms, side, minutes))
    return rows


def make_spec(name: str, minutes: int, signals) -> StrategySpec:
    return StrategySpec(name, VERSION, strategy_config(name, minutes), signals)
