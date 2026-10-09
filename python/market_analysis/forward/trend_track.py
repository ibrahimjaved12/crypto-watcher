"""Forward track of the daily trend Mode B variants (#239 P14, #222): portfolio mode, pure and stateless.

Mode B is a daily weight stream, not discrete trades, so the forward test of the K = 9 frozen
``benchmark.trend`` variants is a hypothetical vol-targeted portfolio per variant, evaluated one
completed UTC day at a time on the live Binance daily feed. The signal, sizing and band functions
are the backtest's own (``trend.signal_inputs``, ``trend.size``, ``trend.apply_band``); nothing is
copied, so a variant here is exactly the variant of the development / validation reports.

Timing (as the backtest). The weight held on day ``t`` (open to close) uses closes through day
``t - 1`` only: a close of day ``d`` never affects the weight of day ``d``, only of ``d + 1``.
Day net of one symbol (fraction of equity)::

    w_t * (close_t / open_t - 1) - 11 bp * |w_t - w_{t-1}| - w_t * sum(funding rates)

over settlements ``open_t < calc_time <= open_t + 1 day``. The portfolio day is the equal-weight
mean over the active symbols (evaluable day and a defined weight, or a non-zero weight being
closed), 0 on a day without one; it is recorded in integer parts per million (``trend.to_ppm``)
and is byte-equal to ``trend.evaluate_trend``'s daily stream for the same closes (tested).

A MISSING symbol-day (no completed kline) is never filled: the symbol is flat in every weight
whose lookback contains it and is not evaluated that day. Funding that is unknown (fetch failed
or not covered) finalises nothing; the caller records ``funding_unavailable``.

Controls (recorded from day one): the vol-targeted buy-and-hold of each (sizing, sigma target)
pair (the benchmark of the backtest's alpha test, S = 1, same sizing, band and costs) and a
long-only equal-weight portfolio (weight 1 per symbol). The circular-shift placebo needs the
whole sample and is not computable forward.

Hypothetical track: no margin, liquidation or position-size limits. No variant has a validated edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from fractions import Fraction

import numpy as np

from .. import data_lake
from ..benchmark import trend
from ..benchmark.canonical import content_hash
from ..benchmark.power import min_detectable_edge_per_day

TRACK_VERSION = "trend-track-v1"
DAY_MS = trend.DAY_MS
PPM = trend.PPM
TRACK_START_MS = data_lake.month_bounds_ms("2026-10")[0]  # forward live from 2026-10 (owner decision)
MIN_HISTORY_DAYS = 400  # 360-day channel + the rvfilter's 30 + 365 window need about 396 closes
INITIAL_EQUITY_PPM = PPM
PRICE_SCALE = 10 ** 8
_NAN = float("nan")


@dataclass(frozen=True)
class Track:
    name: str
    kind: str                         # "variant" | "buy_hold_vt" | "equal_weight_long"
    variant: trend.TrendVariant | None = None

    def config(self) -> dict:
        symbols = sorted(data_lake.SYMBOLS)
        if self.kind == "variant":
            return self.variant.config(symbols)
        if self.kind == "buy_hold_vt":
            return {"control": "vol-targeted buy-and-hold (S = 1)", "sizing": self.variant.sizing,
                    "sigma_target": self.variant.sigma_target, "band": "0.10", "cap": "2",
                    "cost_bp_per_unit_turnover": trend.COST_BP_PER_UNIT_TURNOVER, "universe": symbols}
        return {"control": "long-only equal weight (w = 1 per symbol)", "band": "0.10",
                "cost_bp_per_unit_turnover": trend.COST_BP_PER_UNIT_TURNOVER, "universe": symbols}


def _control_name(variant: trend.TrendVariant) -> str:
    return f"bh_vt_{variant.sizing}_{variant.sigma_target.replace('0.', '')}"


def _tracks() -> tuple:
    variants = tuple(Track(variant.name, "variant", variant) for variant in trend.VARIANTS)
    controls = {}
    for variant in trend.VARIANTS:  # one control per (sizing, sigma target), as the backtest's controls dict
        controls.setdefault(_control_name(variant), Track(_control_name(variant), "buy_hold_vt", variant))
    return variants + tuple(controls.values()) + (Track("ew_long", "equal_weight_long"),)


TRACKS = _tracks()
TRACKS_BY_NAME = {track.name: track for track in TRACKS}
CONTROL_OF = {variant.name: _control_name(variant) for variant in trend.VARIANTS}


def params_hash() -> str:
    return content_hash({"version": TRACK_VERSION, "strategy_version": trend.STRATEGY_VERSION,
                         "tracks": {track.name: track.config() for track in TRACKS}})


# ---------------------------------------------------------------- weights


def _as_track(item) -> Track:
    if isinstance(item, Track):
        return item
    if isinstance(item, trend.TrendVariant):
        return Track(item.name, "variant", item)
    return TRACKS_BY_NAME[item]


def weight_path(track: Track, close) -> tuple[np.ndarray, np.ndarray]:
    """(weights, defined) per position day of ``close``'s index: weights[t] uses closes through t-1.

    Pass the closes through the decision day plus one NaN to obtain the next day's weight.
    """
    close = np.asarray(close, dtype=np.float64)
    if track.kind == "equal_weight_long":
        target = np.where(np.isnan(trend.shift1(close)), _NAN, 1.0)
        block = None
    else:
        inputs = trend.signal_inputs(track.variant, close)
        if track.kind == "buy_hold_vt":  # trend.real_path(constant_signal=True)
            inputs = trend.SignalInputs(np.where(np.isnan(inputs.sigma), _NAN, 1.0), inputs.sigma, None)
        target = trend.size(inputs.signal, inputs.sigma, track.variant.target)
        block = inputs.block
    return trend.apply_band(target, block), ~np.isnan(target)


def target_weights(variants, daily_closes_by_symbol: dict, decision_day_ms: int) -> dict:
    """Weights held on day ``decision_day + 1`` from closes through ``decision_day`` (the backtest's shift).

    ``daily_closes_by_symbol``: symbol -> (start_ms, closes), NaN on MISSING days. Closes after the
    decision day are ignored. Returns {track: {symbol: (weight, defined)}}.
    """
    out = {}
    for item in variants:
        track = _as_track(item)
        weights = {}
        for symbol, (start_ms, closes) in daily_closes_by_symbol.items():
            index = (decision_day_ms - start_ms) // DAY_MS
            if index < 0:
                weights[symbol] = (0.0, False)
                continue
            known = np.asarray(closes, dtype=np.float64)[:index + 1]
            known = np.concatenate([known, np.full(index + 1 - len(known), _NAN), [_NAN]])
            path, defined = weight_path(track, known)
            weights[symbol] = (float(path[-1]), bool(defined[-1]))
        out[track.name] = weights
    return out


# ---------------------------------------------------------------- portfolio step


def initial_state(track: str, previous_weights: dict | None = None) -> dict:
    return {"version": TRACK_VERSION, "track": track, "last_day_ms": None, "equity_ppm": INITIAL_EQUITY_PPM,
            "n_days": 0, "sum_ppm": 0, "sum_sq_ppm": 0, "peak_equity_ppm": INITIAL_EQUITY_PPM,
            "max_drawdown_ppm": 0, "weights": dict(previous_weights or {}), "mu_min_daily": None}


def step_portfolio(state: dict, day_ms: int, weights: dict, day_returns: dict, funding: dict,
                   turnover_cost_bp: int = trend.COST_BP_PER_UNIT_TURNOVER) -> tuple[dict, dict]:
    """One completed day of one track -> (new state, ledger row).

    ``weights``: symbol -> (w_t, defined), in the portfolio's symbol order; ``day_returns``: symbol ->
    close/open - 1 or None (MISSING); ``funding``: symbol -> sum of rates paid by a position held that
    day, or None (unknown: the symbol is not evaluable). The previous weight is ``state['weights']``.
    Same float operations, in the same order, as ``trend.net`` and ``trend.portfolio``.
    """
    if state["last_day_ms"] is not None and day_ms != state["last_day_ms"] + DAY_MS:
        raise ValueError("days must be stepped contiguously")
    cost = turnover_cost_bp / 10_000
    total, count, turnover, gross = 0, 0, 0.0, 0.0
    held = {}
    for symbol, (weight, defined) in weights.items():
        previous = state["weights"].get(symbol, 0.0)
        ret, paid = day_returns.get(symbol), funding.get(symbol)
        if ret is not None and paid is not None and (defined or previous != 0):
            total += weight * ret - cost * abs(weight - previous) - weight * paid
            count += 1
            turnover += abs(weight - previous)
            gross += abs(weight)
        held[symbol] = weight
    daily = total / count if count else 0.0
    daily_ppm = int(np.rint(np.float64(daily) * PPM))
    equity = round(Fraction(state["equity_ppm"] * (PPM + daily_ppm), PPM))
    peak = max(state["peak_equity_ppm"], equity)
    n = state["n_days"] + 1
    sum_ppm, sum_sq = state["sum_ppm"] + daily_ppm, state["sum_sq_ppm"] + daily_ppm * daily_ppm
    mu_min = None
    if n > 1:
        variance = (sum_sq - Fraction(sum_ppm * sum_ppm, n)) / (n - 1)
        sd = float(variance) ** 0.5 / PPM if variance > 0 else 0.0
        mu_min = min_detectable_edge_per_day(n, sd)
    new_state = {**state, "last_day_ms": day_ms, "equity_ppm": equity, "n_days": n, "sum_ppm": sum_ppm,
                 "sum_sq_ppm": sum_sq, "peak_equity_ppm": peak,
                 "max_drawdown_ppm": max(state["max_drawdown_ppm"], round(Fraction((peak - equity) * PPM, peak))),
                 "weights": held, "mu_min_daily": mu_min}
    row = {"track": state["track"], "day_ms": day_ms, "daily_ppm": daily_ppm, "equity_ppm": equity,
           "turnover_ppm": int(np.rint(turnover / count * PPM)) if count else 0,
           "gross_ppm": int(np.rint(gross / count * PPM)) if count else 0, "symbols_active": count,
           "weights": {symbol: float(weight) for symbol, weight in held.items()}}
    return new_state, row


# ---------------------------------------------------------------- inputs and evaluation


def price_e8(value) -> int:
    scaled = (Decimal(str(value)) * PRICE_SCALE).to_integral_value(rounding=ROUND_HALF_EVEN)
    if scaled <= 0:
        raise ValueError("prices must be positive")
    return int(scaled)


@dataclass(frozen=True)
class SymbolHistory:
    symbol: str
    start_ms: int
    open: np.ndarray    # float of the 10^8-scaled integer price (as trend.prices), NaN on MISSING days
    close: np.ndarray
    funding: tuple      # ((calc_time_ms, Fraction rate), ...) increasing
    funding_from_ms: int | None
    funding_to_ms: int | None
    funding_available: bool

    def index(self, day_ms: int) -> int:
        return (day_ms - self.start_ms) // DAY_MS

    def day_return(self, day_ms: int):
        i = self.index(day_ms)
        if not 0 <= i < len(self.close) or np.isnan(self.close[i]) or np.isnan(self.open[i]):
            return None
        return float(self.close[i] / self.open[i] - 1.0)

    def day_funding(self, day_ms: int):
        """Sum of rates with open < calc <= open + 1 day (``trend.day_funding``), None when not covered."""
        if (not self.funding_available or self.funding_from_ms is None or self.funding_to_ms is None
                or not self.funding_from_ms <= day_ms or day_ms + DAY_MS > self.funding_to_ms):
            return None
        return float(sum((rate for calc, rate in self.funding if day_ms < calc <= day_ms + DAY_MS), Fraction(0)))


def symbol_history(item: dict, through_day_ms: int) -> SymbolHistory | None:
    bars = sorted(item["bars"], key=lambda bar: bar["day_ms"])
    previous = None
    for bar in bars:
        if bar["day_ms"] % DAY_MS or (previous is not None and bar["day_ms"] <= previous):
            raise ValueError("daily bars must be distinct UTC days")
        previous = bar["day_ms"]
    bars = [bar for bar in bars if bar["day_ms"] <= through_day_ms]
    funding = tuple(sorted((int(event["calc_time_ms"]), Fraction(Decimal(str(event["rate"]))))
                           for event in item.get("funding", ())))
    if not bars:
        return None
    start = bars[0]["day_ms"]
    days = (through_day_ms - start) // DAY_MS + 1
    opens, closes = np.full(days, _NAN), np.full(days, _NAN)
    for bar in bars:
        i = (bar["day_ms"] - start) // DAY_MS
        opens[i], closes[i] = float(price_e8(bar["open"])), float(price_e8(bar["close"]))
    return SymbolHistory(item["symbol"], start, opens, closes, funding, item.get("funding_from_ms"),
                         item.get("funding_to_ms"), bool(item.get("funding_available", True)))


def evaluate(symbols: list, through_day_ms: int, states: dict | None = None,
             track_start_ms: int = TRACK_START_MS) -> dict:
    """Finalise every completed day after each track's state, through ``through_day_ms``.

    Returns new ledger rows, the weight rows of the evaluated days and of the next day (decided from
    the closes through ``through_day_ms``), the new states and ``funding_unavailable``. Idempotent:
    the same inputs give the same rows; a day already in a state is never evaluated again.
    """
    if through_day_ms % DAY_MS or track_start_ms % DAY_MS:
        raise ValueError("days must start at 00:00 UTC")
    histories = [history for history in (symbol_history(item, through_day_ms) for item in symbols) if history]
    order = [history.symbol for history in histories]
    if len(set(order)) != len(order):
        raise ValueError("duplicate symbol")
    states = dict(states or {})
    funding_unavailable = [history.symbol for history in histories if not history.funding_available]
    ledger, weight_rows, reasons, new_states = [], [], {}, {}
    for track in TRACKS:
        paths = {}
        for history in histories:  # closes through the last day plus one NaN: the next day's weight
            paths[history.symbol] = weight_path(track, np.concatenate([history.close, [_NAN]]))

        def weights_on(day_ms: int) -> dict:
            out = {}
            for history in histories:
                i = history.index(day_ms)
                weights, defined = paths[history.symbol]
                out[history.symbol] = ((float(weights[i]), bool(defined[i])) if 0 <= i < len(weights)
                                       else (0.0, False))
            return out

        state = states.get(track.name)
        if state is None:
            before = weights_on(track_start_ms - DAY_MS)
            state = initial_state(track.name, {symbol: weight for symbol, (weight, _) in before.items()})
        elif state.get("version") != TRACK_VERSION or state.get("track") != track.name:
            raise ValueError("state of another track or version")
        day = track_start_ms if state["last_day_ms"] is None else state["last_day_ms"] + DAY_MS
        first_new = day
        while day <= through_day_ms:
            day_returns = {history.symbol: history.day_return(day) for history in histories}
            funding = {history.symbol: history.day_funding(day) for history in histories}
            unknown = [symbol for symbol, value in funding.items() if value is None]
            if unknown:
                reasons[track.name] = f"funding_unavailable:{','.join(unknown)}"
                break
            weights = weights_on(day)
            state, row = step_portfolio(state, day, weights, day_returns, funding)
            ledger.append(row)
            day += DAY_MS
        new_states[track.name] = state
        for day_ms in range(first_new, through_day_ms + 2 * DAY_MS, DAY_MS):  # new days and the next day
            weights = weights_on(day_ms)
            weight_rows.append({"track": track.name, "day_ms": day_ms, "decided_from_close_ms": day_ms - DAY_MS,
                                "weights": {symbol: w for symbol, (w, _) in weights.items()},
                                "defined": {symbol: d for symbol, (_, d) in weights.items()}})
    finalised = [state["last_day_ms"] for state in new_states.values()]
    return {"schema_version": 1, "versions": {"trend_track": TRACK_VERSION, "strategy": trend.STRATEGY_VERSION},
            "params_hash": params_hash(), "symbols": order, "track_start_ms": track_start_ms,
            "tracks": [{"name": track.name, "kind": track.kind,
                        "control": CONTROL_OF.get(track.name), "config_hash": content_hash(track.config())}
                       for track in TRACKS],
            "through_day_ms": min(finalised) if finalised and None not in finalised else None,
            "ledger": ledger, "weights": weight_rows, "states": new_states,
            "funding_unavailable": funding_unavailable, "reasons": reasons,
            "assumptions": ["hypothetical-no-margin", "no-liquidation-model", "taker-cost-11bp-per-unit-turnover",
                            "equal-weight-active-symbols"]}
