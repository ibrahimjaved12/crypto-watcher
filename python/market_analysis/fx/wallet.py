"""Forex CFD paper wallet (#374): pure, append-only, same step/ledger shape as the crypto wallet.

``step(state, events, config) -> (state, ledger)`` with the crypto ledger line fields, so the existing
``paper_ledger`` table can store it later (types ``open_fee``, ``pnl``, ``close_fee``, ``funding``,
``liquidation``, ``rejected``; swap is a ``funding`` line with ``kind: "swap"``).

Sizing is in base units from the broker's contract size (lots = units / contract_size), floored to the
lot step. Margin is ``notional / leverage`` in the ACCOUNT currency (the quote currency is converted
with the event's ``quote_to_account``). Commission is per lot and side. The spread is already inside
the setup's fills. Stop-out is isolated per position, which is conservative: the real account is
cross-margin. Not modelled yet: mark-to-market equity and portfolio risk caps beyond total exposure.

Account amounts are integers in 10**-8 of the account currency, rounded half-even once per ledger line,
and the same integer moves the balance (``initial + sum(amounts) == balance``). Prices in setups and
events are scaled to 10**8.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Dict, Optional

from ..benchmark.canonical import exact_from_str, exact_to_str
from .bars import FLAG_PRESENT, FxBarSeries
from .costs import (FX_COST_MODEL_V1, FxContract, FxCostModel, SwapRates, commission_cash,
                    max_admissible_leverage, required_margin, stop_out_price, swap_cash_quote, units_for_risk)

WALLET_VERSION = "fx-wallet-v1"
E8 = 10 ** 8
# A rollover applies to positions held just before it, including ones that close or stop out at that
# instant (exit exactly at rollover is charged, entry exactly at it is not), so swap goes first.
_ORDER = {"swap": 0, "liquidation": 1, "close": 2, "open": 3}
_EXIT_AFTER_OWN_OPEN = 4  # an exit in the same minute as its own open (entry-minute stop/target)


@dataclass(frozen=True)
class FxWalletConfig:
    initial_balance_e8: int = 1000 * E8
    risk_fraction: Fraction = Fraction(1, 100)
    max_positions: int = 1000  # effectively no position-count cap (owner, 2026-10-19); the reason stays configurable
    max_exposure_multiple: Fraction = Fraction(10)  # total open notional <= 10 x balance (UNVERIFIED placeholder)
    model: FxCostModel = FX_COST_MODEL_V1
    contracts: tuple = ()  # (symbol, FxContract) pairs
    assumptions: tuple = ("fx-wallet: isolated per-position stop-out; the real account is cross-margin (conservative)",)

    def __post_init__(self):
        for name in ("initial_balance_e8", "max_positions"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive int")
        for name in ("risk_fraction", "max_exposure_multiple"):
            value = getattr(self, name)
            if type(value) not in (int, Fraction) or value <= 0:
                raise ValueError(f"{name} must be a positive int or Fraction")
            object.__setattr__(self, name, Fraction(value))
        if not isinstance(self.model, FxCostModel):
            raise ValueError("model must be an FxCostModel")
        pairs = tuple(self.contracts)
        if not all(isinstance(p, tuple) and len(p) == 2 and type(p[0]) is str and isinstance(p[1], FxContract)
                   for p in pairs) or len({p[0] for p in pairs}) != len(pairs):
            raise ValueError("contracts must be unique (symbol, FxContract) pairs")
        object.__setattr__(self, "contracts", pairs)

    def contract(self, symbol: str) -> Optional[FxContract]:
        return dict(self.contracts).get(symbol)

    def to_record(self) -> dict:
        return {"version": WALLET_VERSION, "initial_balance_e8": self.initial_balance_e8,
                "risk_fraction": exact_to_str(self.risk_fraction), "max_positions": self.max_positions,
                "max_exposure_multiple": exact_to_str(self.max_exposure_multiple),
                "cost_model": self.model.identity(),
                "contracts": [{"symbol": symbol, "contract_size": exact_to_str(c.contract_size),
                               "tick_size": exact_to_str(c.tick_size), "min_lot": exact_to_str(c.min_lot),
                               "lot_step": exact_to_str(c.lot_step), "base": c.base, "quote": c.quote}
                              for symbol, c in self.contracts],
                "assumptions": list(self.assumptions)}


def initial_state(config: FxWalletConfig = FxWalletConfig()) -> dict:
    return {"version": WALLET_VERSION, "balance_e8": config.initial_balance_e8, "used_margin_e8": 0, "seq": 0,
            "positions": {}, "done": [], "swaps": []}


def position_account_value(position: dict, bid: int, spread: int, quote_to_account) -> Fraction:
    """Unrealized P&L in account currency at the current executable exit price (informational)."""
    side = position["side"]
    exit_price = bid if side == 1 else bid + spread
    return (side * exact_from_str(position["units"]) * (exit_price - exact_from_str(position["entry_fill"]))
            / E8 * Fraction(quote_to_account))


def _round(value: Fraction) -> int:
    return round(value)


def step(state: dict, events, config: FxWalletConfig = FxWalletConfig()) -> tuple:
    """Apply events in (ms, swap < liquidation < close < open, id) order; returns (new state, ledger).

    An exit at the same ms as its own open in this batch runs after that open. Each (position,
    rollover) pair is charged once, across batches too (``state["swaps"]``).
    """
    state = {"version": WALLET_VERSION, "balance_e8": int(state["balance_e8"]),
             "used_margin_e8": int(state["used_margin_e8"]), "seq": int(state["seq"]),
             "positions": {key: dict(value) for key, value in state["positions"].items()},
             "done": list(state["done"]), "swaps": list(state.get("swaps", []))}
    done = set(state["done"])
    swapped = set(state["swaps"])
    ledger = []
    model = config.model

    def entry(entry_type: str, ms: int, amount: int, **fields) -> None:
        state["seq"] += 1
        state["balance_e8"] += amount
        ledger.append({"seq": state["seq"], "ms": ms, "type": entry_type, "amount_e8": amount,
                       "balance_e8": state["balance_e8"], "assumptions": list(config.assumptions), **fields})

    def finish(setup_id: str) -> None:
        done.add(setup_id)
        state["done"].append(setup_id)

    def commission(position: dict) -> Fraction:
        return commission_cash(exact_from_str(position["units"]), config.contract(position["symbol"]), model)

    for event in events:
        if event.get("type") not in _ORDER:
            raise ValueError(f"unknown wallet event {event.get('type')!r}")
    opened_at = {(ev["setup"]["setup_id"], ev["ms"]) for ev in events if ev["type"] == "open"}

    def order(ev) -> tuple:
        priority = _ORDER[ev["type"]]
        if ev["type"] in ("close", "liquidation") and (ev["setup_id"], ev["ms"]) in opened_at:
            priority = _EXIT_AFTER_OWN_OPEN
        return (ev["ms"], priority, ev.get("setup_id") or ev.get("setup", {}).get("setup_id") or ev.get("symbol", ""))

    ordered = sorted(events, key=order)
    for event in ordered:
        kind, ms = event["type"], event["ms"]
        if kind == "open":
            setup = event["setup"]
            setup_id = setup["setup_id"]
            if setup_id in done or setup_id in state["positions"]:
                continue
            base = {"setup_id": setup_id, "symbol": setup["symbol"]}
            contract = config.contract(setup["symbol"])
            conv = exact_from_str(event["quote_to_account"])
            reason = None
            if setup.get("status") != "T":
                reason = "not_tradeable"
            elif contract is None:
                reason = "no_contract"
            else:
                fill = exact_from_str(setup["entry_fill"])
                distance = exact_from_str(setup["risk_distance"])
                risk = Fraction(state["balance_e8"], E8) * config.risk_fraction
                units = units_for_risk(risk, distance / E8, conv, contract)
                leverage = max_admissible_leverage(fill, distance, model)
                if units is None:
                    reason = "below_min_lot"
                elif leverage is None:
                    reason = "no_admissible_leverage"
                else:
                    notional_e8 = units * fill * conv  # units * (fill / 10**8) * conv * 10**8
                    margin_e8 = required_margin(units, fill / E8, leverage, conv) * E8
                    fee_e8 = commission_cash(units, contract, model) * E8
                    exposure = sum(exact_from_str(p["notional_e8"]) for p in state["positions"].values())
                    if len(state["positions"]) >= config.max_positions:
                        reason = "cap:max_positions"
                    elif exposure + notional_e8 > config.max_exposure_multiple * state["balance_e8"]:
                        reason = "cap:total_exposure"
                    elif state["balance_e8"] - state["used_margin_e8"] < margin_e8 + fee_e8:
                        reason = "insufficient_margin"
            if reason is not None:
                entry("rejected", ms, 0, reason=reason, **base)
                finish(setup_id)
                continue
            margin = ceil(margin_e8)
            state["used_margin_e8"] += margin
            state["positions"][setup_id] = {
                "symbol": setup["symbol"], "side": setup["side"], "entry_ms": setup["entry_ms"],
                "stop": setup["stop"], "target": setup["target"], "units": exact_to_str(units),
                "entry_fill": exact_to_str(fill), "leverage": leverage,
                "stop_out_price": exact_to_str(stop_out_price(setup["side"], fill, leverage, model)),
                "margin_e8": margin, "notional_e8": exact_to_str(notional_e8),
                "contract_size": exact_to_str(contract.contract_size),
                "quote_to_account_entry": exact_to_str(conv)}
            entry("open_fee", ms, -_round(fee_e8), leverage=leverage, units=exact_to_str(units), margin_e8=margin,
                  **base)
        elif kind in ("close", "liquidation"):
            setup_id = event["setup_id"]
            position = state["positions"].get(setup_id)
            if position is None:
                continue  # unknown, already closed or already stopped out
            side, units = position["side"], exact_from_str(position["units"])
            fill = exact_from_str(position["entry_fill"])
            conv = exact_from_str(event["quote_to_account"])
            base = {"setup_id": setup_id, "symbol": position["symbol"]}
            if kind == "liquidation":
                level = exact_from_str(position["stop_out_price"])
                loss = side * units * (level - fill) / E8 * conv * E8
                loss = max(loss, -Fraction(position["margin_e8"]))  # never more than the isolated margin
                entry("liquidation", ms, _round(loss), **base)
            else:
                exit_fill = exact_from_str(event["exit_fill"])
                pnl = side * units * (exit_fill - fill) / E8 * conv * E8
                entry("pnl", ms, _round(pnl), outcome=event.get("outcome"), **base)
                entry("close_fee", ms, -_round(commission(position) * E8), **base)
            state["used_margin_e8"] -= position["margin_e8"]
            del state["positions"][setup_id]
            # A closed position's rollover keys can never matter again; prune them so the state stays small.
            prefix = setup_id + "|"
            state["swaps"] = [key for key in state["swaps"] if not key.startswith(prefix)]
            swapped = {key for key in swapped if not key.startswith(prefix)}
            finish(setup_id)
        elif kind == "swap":
            rates = SwapRates(exact_from_str(event["long_per_unit"]), exact_from_str(event["short_per_unit"]))
            conv = exact_from_str(event["quote_to_account"])
            for setup_id, position in sorted(state["positions"].items()):
                key = f"{setup_id}|{ms}"
                if position["symbol"] != event["symbol"] or position["entry_ms"] >= ms or key in swapped:
                    continue
                swapped.add(key)
                state["swaps"].append(key)
                cash = swap_cash_quote(position["side"], exact_from_str(position["units"]), event["multiplier"],
                                       rates, model)
                entry("funding", ms, _round(cash * conv * E8), setup_id=setup_id, symbol=position["symbol"],
                      kind="swap", multiplier=event["multiplier"])
    return state, ledger


def stop_out_events(positions: Dict[str, dict], bars: FxBarSeries, resolutions: Dict[str, object],
                    quote_to_account: str) -> list:
    """First stop-out minute per open position of ``bars.symbol``, scanned from its entry up to its
    resolution's ``exit_ms`` (or the last bar); long on the bid low, short on the ask high."""
    events = []
    for setup_id, position in sorted(positions.items()):
        if position["symbol"] != bars.symbol:
            continue
        level = exact_from_str(position["stop_out_price"])
        first = max(position["entry_ms"], bars.start_ms)
        if first >= bars.end_ms:
            continue
        resolution = resolutions.get(setup_id)
        exit_ms = getattr(resolution, "exit_ms", None) if resolution is not None else None
        last = bars.minutes - 1 if exit_ms is None else min(bars.minutes - 1, (exit_ms - bars.start_ms) // 60_000)
        for i in range(bars.index_of(first), last + 1):
            if bars.flags[i] != FLAG_PRESENT:
                continue
            if position["side"] == 1:
                hit = bars.low[i] <= floor(level)
            else:
                hit = bars.high[i] + bars.spread[i] >= ceil(level)
            if hit:
                events.append({"type": "liquidation", "ms": bars.open_time(i), "setup_id": setup_id,
                               "quote_to_account": quote_to_account})
                break
    return events


__all__ = ["E8", "FxWalletConfig", "WALLET_VERSION", "initial_state", "position_account_value", "step",
           "stop_out_events"]
