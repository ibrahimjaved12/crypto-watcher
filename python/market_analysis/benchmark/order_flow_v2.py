"""Order-flow threshold ladder ``of-v2`` (#188 of-v2a): the of-v1 ``cum240`` rule at four thresholds.

A separate family so the of-v1 family, its strategies and every of-v1 identity and
hash stay byte-identical. Each strategy is ``order_flow.cum240`` (240-minute
cumulative taker imbalance, robust z over 30 days of hourly values, crossing of
``|z| >= c`` from below, disarmed until ``|z| < 1``, 240-minute cooldown) with the
crossing threshold ``c`` = 2, 5/2, 3 or 7/2; ``c = 2`` reproduces ``of_cum240_4h``
exactly (the replication cell). The signal code is reused, not copied.

Geometry: the 240-minute horizon, k = 2 and rr indices 1, 2, 3 (rr 3/2, 2, 3) on the
existing lb1 labels, so a question has K = 4 strategies x 3 targets = 12 variants.
Disarm and cooldown state depend on the threshold, so the signal sets of different
thresholds are not nested in general.
"""
from __future__ import annotations

from fractions import Fraction

from .bars import BarSeries
from .canonical import exact_to_str
from .funding import FundingSeries
from .order_flow import MAD_SCALE, cum240
from .runner import StrategySpec

VERSION = "of-v2"
HORIZON = 240
K_VALUES = ["2"]
RR_INDICES = [1, 2, 3]  # rr_grid[1..3] = 3/2, 2, 3
_MINUTE = 60_000

_COMMON = {"rearm_below": Fraction(1), "window_min": 240, "decision_step_min": 60, "history_days": 30,
           "min_history": 500, "cooldown_min": 240}
# name -> (kind, parameters, rule): the same tuple shape as order_flow.STRATEGIES
STRATEGIES = {
    f"of2_cum240_c{label}": ("cum240", {"theta": theta, **_COMMON}, cum240)
    for label, theta in (("20", Fraction(2)), ("25", Fraction(5, 2)), ("30", Fraction(3)), ("35", Fraction(7, 2)))
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
        raise ValueError(f"unknown order-flow-v2 strategy {name!r}")
    if minutes != HORIZON:
        raise ValueError(f"order-flow-v2 strategies are {HORIZON}-minute only")
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
