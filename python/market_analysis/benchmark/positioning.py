"""Positioning family ``positioning-v1`` (#188): fade the account long/short ratio at 240 minutes.

Pre-registered from the positioning screen (development): ``global-ls`` (``count_long_short_ratio``,
the account-weighted long/short ratio of the Binance metrics archive) had a negative slope on
forward returns (day-clustered t about -3.6 at 60 m and -3.1 at 240 m); the other three positioning
series showed nothing. That is an exploratory finding on the same data, so the DIRECTION IS FIXED as
fade before any evaluation: SHORT when the robust z is high (crowded longs), LONG when it is low. The
question is judged on development first, then validation; the hidden stretch is untouched.

Value and point in time: metrics rows come from ``metrics_lake.load_symbol_metrics_range`` (hidden
months refused before any file is opened). A row stamped ``create_time`` is usable only from
``create_time + 5 min`` (``metrics_lake.usable_from_ms``). Decisions are hourly (``signal_ms`` on the
hour, on the 240 m label grid); the value at decision t is the row usable exactly at t (stamped
t - 5 min). A missing row or empty value DROPS the decision (no z, no history value, no state change):
it is never filled from an older row.

Robust z: ``order_flow.robust_z`` of the value against the same-phase history, i.e. the values at the
previous hourly decisions inside ``[t - 30 days, t)`` (at least 500 of them; the literal same hour of
day would give at most 30). This is exactly the screen's ``global-ls`` z (``screen.positioning_value``).

Rule (level trigger with re-arming, never a crossing): while armed, ``z >= theta`` gives a SHORT
signal and ``z <= -theta`` a LONG signal; a signal disarms the strategy until ``|z| < 1``; no signal
less than 240 minutes after the previous one of the same symbol and strategy (a suppressed trigger
changes no state). Strategies ``pos_gls_fade_c20/c25/c30`` use theta 2, 5/2, 3.

Geometry: 240-minute horizon, k = 2, rr indices 1, 2, 3 (rr 3/2, 2, 3): K = 3 x 3 = 9 variants. Labels:
the family's questions use the ``ewma-robust-hcal`` sigma model (lb3h, question-v2, P16).
"""
from __future__ import annotations

from fractions import Fraction
from math import isnan

from .. import data_lake
from ..metrics_lake import PERIOD_MS, POINT_IN_TIME_LAG_PERIODS, load_symbol_metrics_range
from .canonical import exact_to_str
from .order_flow import MAD_SCALE, TrailingHistory, robust_z
from .runner import StrategySpec

VERSION = "positioning-v1"
HORIZON = 240
K_VALUES = ["2"]
RR_INDICES = [1, 2, 3]  # rr_grid[1..3] = 3/2, 2, 3
SIGMA_MODEL = "ewma-robust-hcal"
SERIES = "count_long_short_ratio"
METRICS_REVISION = 1  # mx-SYMBOL-MONTH-r1 releases
_MINUTE = data_lake.MINUTE_MS
_HOUR = 60 * _MINUTE
_DAY = 1440 * _MINUTE

_COMMON = {"rearm_below": Fraction(1), "decision_step_min": 60, "history_days": 30, "min_history": 500,
           "cooldown_min": 240}
STRATEGIES = {
    f"pos_gls_fade_c{label}": ("gls_fade", {"theta": theta, **_COMMON})
    for label, theta in (("20", Fraction(2)), ("25", Fraction(5, 2)), ("30", Fraction(3)))
}


def strategy_config(name: str, minutes: int) -> dict:
    """Every parameter, with Fractions as exact text, plus the series, lag and fixed label geometry."""
    kind, parameters = STRATEGIES[name]
    values = {key: exact_to_str(value) if isinstance(value, Fraction) else value for key, value in parameters.items()}
    return {"kind": kind, "series": SERIES, "direction": "fade", **values, "mad_scale": exact_to_str(MAD_SCALE),
            "usable_lag_min": POINT_IN_TIME_LAG_PERIODS * PERIOD_MS // _MINUTE,
            "metrics_revision": METRICS_REVISION, "timeframe_min": minutes, "sigma_model": SIGMA_MODEL,
            "k_values": list(K_VALUES), "rr_indices": list(RR_INDICES)}


def load_metrics(metrics_dir, symbol: str, last_month: str, horizon: int, token, gate):
    """Guarded metrics of ``data_lake.FIRST_MONTH..last_month`` (hidden months need a verified token)."""
    return load_symbol_metrics_range(metrics_dir, symbol, data_lake.FIRST_MONTH, last_month, token=token, gate=gate)


def value_at(metrics, decision_ms: int):
    """The ratio usable exactly at the decision (row stamped decision - 5 min), or None (dropped)."""
    index = metrics.index_at(decision_ms)
    if index is None:
        return None
    value = metrics.columns[SERIES][index]
    return None if isnan(value) else float(value)


def z_series(metrics, *, decision_step_min: int, history_days: int, min_history: int) -> list:
    """[(signal_ms, z or None)] at every hourly decision covered by the metrics grid."""
    step = decision_step_min * _MINUTE
    first = metrics.start_ms + (-metrics.start_ms % step)
    end = metrics.start_ms + metrics.periods * PERIOD_MS
    history, out = TrailingHistory(history_days * _DAY), []
    for signal_ms in range(first, end, step):
        value = value_at(metrics, signal_ms)
        z = None
        if value is not None:
            z = robust_z(history.at(signal_ms), value, min_history)  # previous decisions only
            history.add(signal_ms, value)
        out.append((signal_ms, z))
    return out


def fade_signals(zs, *, theta: Fraction, rearm_below: Fraction, cooldown_ms: int) -> list:
    """[(signal_ms, side)]: SHORT at z >= theta, LONG at z <= -theta while armed, outside the cooldown.

    A dropped decision (z None) changes nothing. A signal disarms until |z| < rearm_below. A trigger
    inside the cooldown changes no state.
    """
    signals, last, armed = [], None, True
    for signal_ms, z in zs:
        if z is None:
            continue
        if not armed and abs(z) < rearm_below:
            armed = True
        if armed and abs(z) >= theta and (last is None or signal_ms - last >= cooldown_ms):
            signals.append((signal_ms, -1 if z > 0 else 1))
            armed, last = False, signal_ms
    return signals


def symbol_signals(name: str, metrics, minutes: int, *, first_ms: int, end_ms: int, label_step_min: int) -> list:
    """(symbol, signal_ms, side, minutes) of one symbol for signals in [first_ms, end_ms)."""
    if name not in STRATEGIES:
        raise ValueError(f"unknown positioning-v1 strategy {name!r}")
    if minutes != HORIZON:
        raise ValueError(f"positioning-v1 strategies are {HORIZON}-minute only")
    if type(first_ms) is not int or type(end_ms) is not int or end_ms < first_ms:
        raise ValueError("signal window must be integer [first_ms, end_ms)")
    _, parameters = STRATEGIES[name]
    zs = z_series(metrics, decision_step_min=parameters["decision_step_min"],
                  history_days=parameters["history_days"], min_history=parameters["min_history"])
    rows = []
    for signal_ms, side in fade_signals(zs, theta=parameters["theta"], rearm_below=parameters["rearm_below"],
                                        cooldown_ms=parameters["cooldown_min"] * _MINUTE):
        if first_ms <= signal_ms < end_ms:
            if signal_ms % (label_step_min * _MINUTE):
                raise ValueError(f"{metrics.symbol} signal {signal_ms} is off the {minutes}m label grid")
            rows.append((metrics.symbol, signal_ms, side, minutes))
    return rows


def make_spec(name: str, minutes: int, signals) -> StrategySpec:
    return StrategySpec(name, VERSION, strategy_config(name, minutes), signals)
