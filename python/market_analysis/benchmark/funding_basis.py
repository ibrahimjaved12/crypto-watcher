"""Funding/basis signal family ``fb-v1`` for the #182 benchmark (slice I1).

Input is the 1-minute Binance premium index (``premium.PremiumSeries``: the
``premium_*`` columns of the data-lake bars, exact integers at scale 10**8) plus
the trade close for price returns. The funding print is NOT used: Binance clamps
it to the 0.01% interest component over a wide premium range, so it carries
little information; the 1-minute premium does.

Every strategy is CONTRARIAN (it fades an extreme): ``side = -sign(z)``.

Decision times are labels-v1 grid times (``signal_ms`` a multiple of the
strategy's step, itself a multiple of the label step); minute ``d = signal_ms /
60_000 - 1`` is the last completed minute and nothing after it is read.

Premium quantity ``x_t``: the mean premium close over the ``window_min = 60``
minutes ending at minute ``d``. It is invalid if any of those minutes has
``FLAG_PREMIUM_MISSING``. The z-score uses the window SUM, which is ``window_min *
x_t``; z is scale-invariant, so it is identical, and integer comparisons are faster.
Robust z (``order_flow.robust_z``): ``(x - median) / (14826/10000 * MAD)`` over the
valid values at earlier decisions of the same step inside ``[t - 30 days, t)``,
defined with at least 500 values and MAD > 0.

- ``fb_prem_z_4h`` (horizon 240): hourly decisions; signal when |z| crosses 2 from
  below (``|z_prev| < 2 <= |z|``), disarmed after a signal until ``|z| < 1``,
  cooldown 240 minutes (``order_flow.crossing_signals``, the same state machine as
  ``of_cum240_4h``).
- ``fb_prem_unconf_1h`` (horizon 60): 15-minute decisions with a 15-minute-step
  history; signal when ``|z| >= 2`` and the 60-minute price return ``close[d] -
  close[d - 60]`` is non-zero with the sign opposite to ``z`` (the premium extreme
  is not confirmed by price). Both closes must be valid (present, no COMPROMISED
  flag); a zero return is no signal. Cooldown 60 minutes.
- ``fb_xs_4h`` (horizon 240): hourly decisions; the hourly z of every frozen symbol
  (as for ``fb_prem_z_4h``, without the crossing rule). Only when ALL six z are
  defined and ``max z - min z >= 3``: short the symbol with the highest z, long the
  one with the lowest. Tie rule: if the highest or the lowest z is shared by more
  than one symbol, that hour gives no signal at all (ambiguity is never resolved
  in favour of a trade). Cooldown 240 minutes per symbol; a leg in cooldown is
  dropped, and the other leg is kept.

Gaps: no value spans a window with a missing premium minute; invalid values
never enter the history; an undefined z restarts the crossing. Each symbol's
history is its own; only ``fb_xs_4h`` combines symbols, and it combines z values
already computed point in time per symbol.
"""
from __future__ import annotations

from collections import defaultdict
from fractions import Fraction

from .. import data_lake
from .bars import CompromisedIndex
from .canonical import exact_to_str
from .order_flow import MAD_SCALE, TrailingHistory, crossing_signals, robust_z, sign
from .premium import PremiumSeries
from .runner import StrategySpec

VERSION = "fb-v1"
# horizon -> (k_values, rr_indices): one (k, rr) per signal
GEOMETRY = {240: (("2",), (1,)), 60: (("2",), (0,))}
_MINUTE = data_lake.MINUTE_MS
_DAY = 1440 * _MINUTE


def premium_z(series: PremiumSeries, *, step_min: int, window_min: int, history_days: int,
              min_history: int) -> list:
    """[(signal_ms, z or None)] at every decision time of the step, in time order."""
    invalid = CompromisedIndex(series, mask=data_lake.FLAG_PREMIUM_MISSING)
    history, out = TrailingHistory(history_days * _DAY), []
    first = -series.start_ms % (step_min * _MINUTE) // _MINUTE
    for e in range(first, series.minutes + 1, step_min):  # e = d + 1
        signal_ms, d = series.start_ms + e * _MINUTE, e - 1
        low = d - window_min + 1
        value = None
        if low >= 0 and not invalid.any_in(low, d):
            value = sum(series.premium_close[low:d + 1])  # window_min x mean premium close
        z = None if value is None else robust_z(history.at(signal_ms), value, min_history)
        if value is not None:
            history.add(signal_ms, value)
        out.append((signal_ms, z))
    return out


def prem_z_cross(series: PremiumSeries, *, theta: Fraction, rearm_below: Fraction, cooldown_min: int,
                 **z_parameters) -> list:
    """[(signal_ms, side)]: contrarian on |z| crossing theta (re-armed below rearm_below)."""
    crossings = crossing_signals(premium_z(series, **z_parameters), theta=theta, rearm_below=rearm_below,
                                 cooldown_ms=cooldown_min * _MINUTE)
    return [(signal_ms, -sign(z)) for signal_ms, z in crossings]


def prem_unconfirmed(series: PremiumSeries, *, theta: Fraction, return_min: int, cooldown_min: int,
                     **z_parameters) -> list:
    """[(signal_ms, side)]: |z| >= theta while the price return over return_min has the opposite sign."""
    signals, last = [], None
    for signal_ms, z in premium_z(series, **z_parameters):
        if z is None or abs(z) < theta:
            continue
        d = (signal_ms - series.start_ms) // _MINUTE - 1
        a = d - return_min
        if a < 0 or not series.price_valid(d) or not series.price_valid(a):
            continue
        move = series.close[d] - series.close[a]
        if move == 0 or sign(Fraction(move)) == sign(z):
            continue
        if last is None or signal_ms - last >= cooldown_min * _MINUTE:
            signals.append((signal_ms, -sign(z)))
            last = signal_ms
    return signals


def cross_section(per_symbol: dict, minutes: int, *, min_spread: Fraction, cooldown_min: int) -> list:
    """(symbol, signal_ms, side, minutes): short the unique top z, long the unique bottom z.

    ``per_symbol[symbol]`` holds that symbol's defined (signal_ms, z); an hour counts
    only if every symbol has a z. A shared top or bottom z gives no signal at all.
    """
    by_time = defaultdict(dict)
    for symbol, items in per_symbol.items():
        for signal_ms, z in items:
            by_time[signal_ms][symbol] = z
    rows, last = [], {}
    for signal_ms in sorted(by_time):
        zs = by_time[signal_ms]
        if len(zs) != len(per_symbol):
            continue
        top, bottom = max(zs.values()), min(zs.values())
        if top - bottom < min_spread:
            continue
        tops = [symbol for symbol, z in zs.items() if z == top]
        bottoms = [symbol for symbol, z in zs.items() if z == bottom]
        if len(tops) > 1 or len(bottoms) > 1:
            continue  # tie: no trade
        for symbol, side in ((tops[0], -1), (bottoms[0], 1)):
            if symbol in last and signal_ms - last[symbol] < cooldown_min * _MINUTE:
                continue
            rows.append((symbol, signal_ms, side, minutes))
            last[symbol] = signal_ms
    return rows


def _defined_z(series: PremiumSeries, **z_parameters) -> list:
    return [(signal_ms, z) for signal_ms, z in premium_z(series, **z_parameters) if z is not None]


_Z = {"window_min": 60, "history_days": 30, "min_history": 500}
# name -> (kind, horizon, parameters, rule); rule None = cross-sectional (combined over symbols)
STRATEGIES = {
    "fb_prem_z_4h": ("prem_z_cross", 240, {"theta": Fraction(2), "rearm_below": Fraction(1), "cooldown_min": 240,
                                           "step_min": 60, **_Z}, prem_z_cross),
    "fb_prem_unconf_1h": ("prem_unconfirmed", 60, {"theta": Fraction(2), "return_min": 60, "cooldown_min": 60,
                                                   "step_min": 15, **_Z}, prem_unconfirmed),
    "fb_xs_4h": ("prem_cross_section", 240, {"min_spread": Fraction(3), "cooldown_min": 240, "step_min": 60, **_Z},
                 None),
}
STRATEGY_HORIZONS = {name: (horizon,) for name, (_, horizon, _, _) in STRATEGIES.items()}
_CROSS_KEYS = ("min_spread", "cooldown_min")


def strategy_config(name: str, minutes: int) -> dict:
    """Every parameter, with Fractions as exact text, plus the fixed label geometry."""
    kind, _, parameters, _ = STRATEGIES[name]
    k_values, rr_indices = GEOMETRY[minutes]
    values = {key: exact_to_str(value) if isinstance(value, Fraction) else value for key, value in parameters.items()}
    return {"kind": kind, **values, "mad_scale": exact_to_str(MAD_SCALE), "side_rule": "contrarian",
            "timeframe_min": minutes, "k_values": list(k_values), "rr_indices": list(rr_indices)}


def _check(name: str, minutes: int, first_ms, end_ms) -> None:
    if name not in STRATEGIES:
        raise ValueError(f"unknown funding/basis strategy {name!r}")
    if minutes not in STRATEGY_HORIZONS[name]:
        raise ValueError(f"{name} is a {STRATEGY_HORIZONS[name][0]}-minute strategy")
    if type(first_ms) is not int or type(end_ms) is not int or end_ms < first_ms:
        raise ValueError("signal window must be integer [first_ms, end_ms)")


def symbol_signals(name: str, series: PremiumSeries, minutes: int, *, first_ms: int, end_ms: int,
                   label_step_min: int) -> list:
    """One symbol's part for [first_ms, end_ms): signal rows, or (signal_ms, z) for fb_xs_4h."""
    _check(name, minutes, first_ms, end_ms)
    _, _, parameters, rule = STRATEGIES[name]
    if parameters["step_min"] % label_step_min:
        raise ValueError(f"{name} step is off the {minutes}m label grid")
    if rule is None:
        z_parameters = {key: value for key, value in parameters.items() if key not in _CROSS_KEYS}
        return [(signal_ms, z) for signal_ms, z in _defined_z(series, **z_parameters)
                if first_ms <= signal_ms < end_ms]
    return [(series.symbol, signal_ms, side, minutes) for signal_ms, side in rule(series, **parameters)
            if first_ms <= signal_ms < end_ms]


def combine(name: str, minutes: int, per_symbol: dict) -> list:
    """All symbols' rows; fb_xs_4h ranks the per-symbol z values here."""
    _, _, parameters, rule = STRATEGIES[name]
    if rule is None:
        return cross_section(per_symbol, minutes, min_spread=parameters["min_spread"],
                             cooldown_min=parameters["cooldown_min"])
    return [row for symbol in per_symbol for row in per_symbol[symbol]]


def make_spec(name: str, minutes: int, signals) -> StrategySpec:
    return StrategySpec(name, VERSION, strategy_config(name, minutes), signals)
