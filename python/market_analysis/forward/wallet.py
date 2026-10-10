"""Paper wallet step (pure, append-only): ``step(state, events, config) -> (state, ledger_entries)``.

Follows docs/futures-simulation.md for an automated fake-money wallet (isolated margin, positions per setup):
- Amounts are integers in 10**-8 USDT (``*_e8``), quantities exact fractions of base units (text).
  Each ledger amount is rounded half-even once and the same integer moves the balance, so
  ``initial + sum(ledger amounts) == balance`` exactly.
- Sizing: risk = ``risk_fraction`` x balance (default 1 % = 1 R); quantity = risk / |entry fill - stop|.
- Leverage chosen by the program: the largest integer leverage (cap ``leverage_cap``) whose
  liquidation price is at least ``liquidation_buffer`` (2) x the stop distance beyond the entry
  (``costs.max_admissible_leverage`` with that buffer and a closing taker-fee reserve).
  Entry fees are paid separately from free balance. No symbol has a maintenance-margin tier
  table in the code yet, so a conservative flat tier (``costs.COST_MODEL_V1.mmr``) is used and every
  ledger line carries ``assumption: flat-tier``.
- Costs: taker fee on entry and exit, a slippage floor (``market_slip_floor_bps``) against the
  position on both fills, funding cash ``-side * qty * mark * rate`` per funding event while open.
- Liquidation: a ``liquidation`` event (from ``liquidation_events``: the mark crosses the wallet's
  liquidation price at or before the resolution minute) closes the position losing its isolated
  margin (``costs.liquidation_loss`` capped at the margin); a later close for it is ignored.
- Rejections (caps, insufficient margin, non-tradeable) are ledger lines with amount 0 and a reason.
- Idempotent: an event for a setup already opened, closed or rejected is ignored. Setups are never
  mutated; the state is a new dict.
Equity for sizing and caps is the realized balance (open positions are not marked to market).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil, floor

from ..benchmark import costs
from ..benchmark.bars import BarSeries
from ..benchmark.canonical import exact_from_str, exact_to_str
from .bars_adapter import MINUTE_MS

WALLET_VERSION = "wallet-v2"
E8 = 10 ** 8
FLAT_TIER = "flat-tier"
_ORDER = {"liquidation": 0, "close": 1, "funding": 2, "open": 3}


@dataclass(frozen=True)
class WalletConfig:
    initial_balance_e8: int = 100 * E8
    risk_fraction: Fraction = Fraction(1, 100)
    max_positions: int = 6
    max_exposure_multiple: Fraction = Fraction(5)  # total open notional <= 5 x balance
    leverage_cap: int = costs.COST_MODEL_V1.leverage_cap
    liquidation_buffer: int = 2
    taker_rate: Fraction = costs.COST_MODEL_V1.taker_rate
    slip_floor_bps: Fraction = costs.COST_MODEL_V1.market_slip_floor_bps
    mmr: Fraction = costs.COST_MODEL_V1.mmr
    assumptions: tuple = (FLAT_TIER,)

    def model(self) -> costs.CostModel:
        return replace(costs.COST_MODEL_V1, mmr=self.mmr, leverage_cap=self.leverage_cap,
                       liq_buffer_multiple=self.liquidation_buffer)

    def to_record(self) -> dict:
        return {"version": WALLET_VERSION, "initial_balance_e8": self.initial_balance_e8,
                "risk_fraction": exact_to_str(self.risk_fraction), "max_positions": self.max_positions,
                "max_exposure_multiple": exact_to_str(self.max_exposure_multiple),
                "leverage_cap": self.leverage_cap, "liquidation_buffer": self.liquidation_buffer,
                "taker_rate": exact_to_str(self.taker_rate), "slip_floor_bps": exact_to_str(self.slip_floor_bps),
                "mmr": exact_to_str(self.mmr), "assumptions": list(self.assumptions)}


def initial_state(config: WalletConfig = WalletConfig()) -> dict:
    return {"version": WALLET_VERSION, "balance_e8": config.initial_balance_e8, "used_margin_e8": 0, "seq": 0,
            "positions": {}, "done": []}


def _round(value: Fraction) -> int:
    return round(value)  # Fraction.__round__: half to even


def _slipped(price: int, side: int, config: WalletConfig, entering: bool) -> Fraction:
    """Fill price with the slippage floor against the position."""
    sign = side if entering else -side
    return Fraction(price) * (1 + sign * config.slip_floor_bps / 10_000)


def position_terms(setup: dict, config: WalletConfig) -> tuple[Fraction, int, Fraction] | None:
    """(entry fill, leverage, liquidation price) of a setup under the wallet's rule; None if no leverage."""
    side, stop = setup["side"], setup["stop"]
    fill = _slipped(setup["p0"], side, config, True)
    # Entry fees are paid from free balance, outside isolated margin. Reserve the
    # closing taker fee at the liquidation mark in addition to maintenance margin.
    model = replace(config.model(), mmr=config.mmr + config.taker_rate)
    leverage = costs.max_admissible_leverage(side, fill, abs(fill - stop), model)
    if leverage is None:
        return None
    return fill, leverage, costs.liquidation_price(side, fill, leverage, model.mmr)


def liquidation_events(positions: dict, bars: BarSeries, resolutions: dict, config: WalletConfig) -> list:
    """Liquidation events of open positions of ``bars.symbol``: the first minute from entry up to the
    resolution's exit minute (or the last bar while unresolved) whose mark crosses the wallet's
    liquidation price (long ``mark_low <= floor(LP)``, short ``mark_high >= ceil(LP)``)."""
    events = []
    for setup_id, position in sorted(positions.items()):
        if position["symbol"] != bars.symbol:
            continue
        lp = exact_from_str(position["liquidation_price"])
        first = max(0, (position["entry_ms"] - bars.start_ms) // MINUTE_MS)
        resolution = resolutions.get(setup_id)
        last = bars.minutes - 1
        if resolution is not None and resolution.get("exit_ms") is not None:
            last = min(last, (resolution["exit_ms"] - bars.start_ms) // MINUTE_MS)
        for i in range(first, last + 1):
            if position["side"] == 1:
                hit = bars.mark_low[i] > 0 and bars.mark_low[i] <= floor(lp)
            else:
                hit = bars.mark_high[i] > 0 and bars.mark_high[i] >= ceil(lp)
            if hit:
                events.append({"type": "liquidation", "ms": bars.open_time(i), "setup_id": setup_id})
                break
    return events


def step(state: dict, events, config: WalletConfig = WalletConfig()) -> tuple[dict, list]:
    """Apply events in (ms, liquidation < close < funding < open, id) order; returns (new state, ledger)."""
    state = {"version": WALLET_VERSION, "balance_e8": int(state["balance_e8"]),
             "used_margin_e8": int(state["used_margin_e8"]), "seq": int(state["seq"]),
             "positions": {key: dict(value) for key, value in state["positions"].items()},
             "done": list(state["done"])}
    done = set(state["done"])
    ledger = []

    def entry(kind: str, ms: int, amount: int, **fields) -> None:
        state["seq"] += 1
        state["balance_e8"] += amount
        ledger.append({"seq": state["seq"], "ms": ms, "type": kind, "amount_e8": amount,
                       "balance_e8": state["balance_e8"], "assumptions": list(config.assumptions), **fields})

    def finish(setup_id: str) -> None:
        done.add(setup_id)
        state["done"].append(setup_id)

    ordered = sorted(events, key=lambda ev: (ev["ms"], _ORDER[ev["type"]], ev.get("setup_id") or ev.get("symbol", "")))
    for event in ordered:
        kind, ms = event["type"], event["ms"]
        if kind == "open":
            setup = event["setup"]
            setup_id = setup["setup_id"]
            if setup_id in done or setup_id in state["positions"]:
                continue
            base = {"setup_id": setup_id, "symbol": setup["symbol"]}
            terms = position_terms(setup, config) if setup.get("status") == "T" else None
            reason = None
            if terms is None:
                reason = "not_tradeable" if setup.get("status") != "T" else "no_admissible_leverage"
            else:
                fill, leverage, lp = terms
                risk = Fraction(state["balance_e8"]) * config.risk_fraction
                qty = risk / abs(fill - setup["stop"])
                notional = qty * fill
                margin = notional / leverage
                fee = notional * config.taker_rate
                exposure = sum(exact_from_str(p["notional_e8"]) for p in state["positions"].values())
                if len(state["positions"]) >= config.max_positions:
                    reason = "cap:max_positions"
                elif exposure + notional > config.max_exposure_multiple * state["balance_e8"]:
                    reason = "cap:total_exposure"
                elif state["balance_e8"] - state["used_margin_e8"] < margin + fee:
                    reason = "insufficient_margin"
            if reason is not None:
                entry("rejected", ms, 0, reason=reason, **base)
                finish(setup_id)
                continue
            fee_e8 = _round(fee)
            margin_e8 = ceil(margin)
            state["used_margin_e8"] += margin_e8
            state["positions"][setup_id] = {
                "symbol": setup["symbol"], "side": setup["side"], "entry_ms": setup["entry_ms"],
                "stop": setup["stop"], "target": setup["target"], "qty": exact_to_str(qty),
                "entry_fill": exact_to_str(fill), "leverage": leverage, "leverage_cap": config.leverage_cap,
                "liquidation_rule": WALLET_VERSION, "liquidation_price": exact_to_str(lp),
                "margin_e8": margin_e8, "notional_e8": exact_to_str(notional)}
            entry("open_fee", ms, -fee_e8, leverage=leverage, qty=exact_to_str(qty), margin_e8=margin_e8, **base)
        elif kind in ("close", "liquidation"):
            setup_id = event["setup_id"]
            position = state["positions"].get(setup_id)
            if position is None:
                continue
            side, qty = position["side"], exact_from_str(position["qty"])
            fill = exact_from_str(position["entry_fill"])
            base = {"setup_id": setup_id, "symbol": position["symbol"]}
            if kind == "liquidation":
                lp = exact_from_str(position["liquidation_price"])
                # Cap once at the stored, rounded-up isolated margin. A per-unit
                # leverage cap would use unrounded margin and can leave 1 e8 behind.
                loss = min(costs.liquidation_loss(side, fill, lp, config.model()) * qty,
                           Fraction(position["margin_e8"]))
                entry("liquidation", ms, -_round(loss), **base)
            else:
                exit_ref = event.get("exit_ref_price")
                exit_fill = fill if exit_ref is None else _slipped(exit_ref, side, config, False)
                entry("pnl", ms, _round(side * qty * (exit_fill - fill)), outcome=event.get("outcome"), **base)
                entry("close_fee", ms, -_round(qty * exit_fill * config.taker_rate), **base)
            state["used_margin_e8"] -= position["margin_e8"]
            del state["positions"][setup_id]
            finish(setup_id)
        elif kind == "funding":
            rate, mark = Fraction(str(event["rate"])), event["mark"]
            for setup_id, position in sorted(state["positions"].items()):
                if position["symbol"] != event["symbol"] or position["entry_ms"] >= ms:
                    continue
                cash = -position["side"] * exact_from_str(position["qty"]) * mark * rate
                entry("funding", ms, _round(cash), setup_id=setup_id, symbol=position["symbol"],
                      rate=event["rate"])
        else:
            raise ValueError(f"unknown wallet event {kind!r}")
    return state, ledger


def wallet_step(state: dict, events, config: WalletConfig = WalletConfig()) -> tuple[dict, list]:
    """Pure portfolio composition of ordered per-symbol market events.

    Merge transport records by (ms, symbol, sequence), then execute the existing wallet
    settlement priority. The optional compatibility_order preserves /evaluate's historical
    stable ties for callers whose symbol list is not alphabetical.
    """
    merged = sorted(events, key=lambda event: (event["ms"], event["symbol"], event["sequence"]))
    identities = [(event["symbol"], event["sequence"]) for event in merged]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate per-symbol event sequence")
    if any("compatibility_order" in event for event in merged):
        orders = [event.get("compatibility_order") for event in merged]
        if any(type(order) is not int for order in orders) or len(set(orders)) != len(orders):
            raise ValueError("compatibility_order must be present and unique on every event")
        merged.sort(key=lambda event: event["compatibility_order"])
    # Historically opens have no symbol sort key; the symbol lives inside their setup.
    execution = [{key: value for key, value in event.items()
                  if not (event["type"] == "open" and key == "symbol")} for event in merged]
    opened, _ = step(state, [event for event in execution if event["type"] == "open"], config)
    eligible = state["positions"].keys() | opened["positions"].keys()
    execution = [event for event in execution
                 if event["type"] != "liquidation" or event["setup_id"] in eligible]
    return step(state, execution, config)
