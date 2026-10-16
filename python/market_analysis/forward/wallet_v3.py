"""Paper wallet v3 (#36): the wallet-v2 rules plus mark-to-market, exact partial fills and reconciliation inputs.

Pure and append-only, like ``wallet.py`` (which is NOT changed: its ledger is stored and the forward-parity runs
depend on it). Everything new is selected by the run configuration (``WalletV3Config``); with every option at its
default, ``step`` reproduces ``wallet.step`` ledger lines bit for bit (tests/test_forward_wallet_v3.py, "golden").
A material change of the configuration is a NEW run: a state of another version is refused, never converted.

What v3 adds over v2:
- ``mark`` events carry a mark price per symbol. ``account_summary`` derives per-position unrealized P&L and the
  account equity (balance + unrealized) from them; sizing and caps keep using the realized balance unless
  ``sizing_basis == "equity"``.
- ``fill`` events: scale-in (``increase``) and partial exit (``reduce``), quantity-exact, with a weighted average
  entry, margin released pro rata, realized P&L on the reduced part and a fee per fill. A reduction larger than
  the open quantity is a ``rejected`` line (``over_close``); a fill id already seen is ignored.
- ``ledger_links``: every line also names the run, the position and the source event.
- ``state["totals"]``: realized P&L, fees, funding, liquidation losses and (informational) slippage, which sum with
  the initial balance to the balance exactly.
Amounts are integers in 10**-8 USDT (``*_e8``) rounded half-even once per ledger line, quantities exact fractions.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from ..benchmark import costs
from ..benchmark.canonical import exact_from_str, exact_to_str
from . import wallet as v2
from .wallet import E8, _round, _slipped, path_liquidations, position_terms

WALLET_VERSION = "wallet-v3"
SIZING_BASES = ("balance", "equity")
# Same relative order as wallet-v2 (liquidation < close < funding < open); the new kinds slot in around it.
_ORDER = {"liquidation": 0, "close": 1, "funding": 2, "mark": 3, "fill": 4, "open": 5}
_PRE_OPEN = ("open",)


@dataclass(frozen=True)
class WalletV3Config(v2.WalletConfig):
    maker_rate: Fraction = costs.COST_MODEL_V1.maker_rate
    sizing_basis: str = "balance"
    ledger_links: bool = False
    run_id: str = ""

    def __post_init__(self):
        if self.sizing_basis not in SIZING_BASES:
            raise ValueError(f"sizing_basis must be one of {SIZING_BASES}")

    def to_record(self) -> dict:
        record = super().to_record()
        record.update({"version": WALLET_VERSION, "wallet_version": WALLET_VERSION,
                       "maker_rate": exact_to_str(self.maker_rate), "sizing_basis": self.sizing_basis,
                       "ledger_links": self.ledger_links, "run_id": self.run_id})
        return record


def initial_state(config: WalletV3Config = WalletV3Config()) -> dict:
    return {"version": WALLET_VERSION, "balance_e8": config.initial_balance_e8, "used_margin_e8": 0,
            "reserved_e8": 0, "seq": 0, "positions": {}, "done": [], "marks": {}, "fill_ids": [], "orders": {},
            "totals": {"realized_e8": 0, "fees_e8": 0, "funding_e8": 0, "liquidation_e8": 0, "slippage_e8": 0}}


def _copy_state(state: dict) -> dict:
    if state.get("version") != WALLET_VERSION:
        raise ValueError(f"wallet state version {state.get('version')!r} is not {WALLET_VERSION}: "
                         "a material configuration change starts a new run")
    return {"version": WALLET_VERSION, "balance_e8": int(state["balance_e8"]),
            "used_margin_e8": int(state["used_margin_e8"]), "reserved_e8": int(state.get("reserved_e8", 0)),
            "seq": int(state["seq"]),
            "positions": {key: dict(value) for key, value in state["positions"].items()},
            "done": list(state["done"]), "marks": {key: dict(value) for key, value in state.get("marks", {}).items()},
            "fill_ids": list(state.get("fill_ids", [])),
            "orders": {key: dict(value) for key, value in state.get("orders", {}).items()},
            "totals": dict(state["totals"])}


# ---------------------------------------------------------------- mark to market


def position_unrealized(position: dict, price: int) -> int:
    """Unrealized P&L (e8) of a position at ``price`` (an integer at 10**8): side * qty * (price - entry)."""
    return _round(position["side"] * exact_from_str(position["qty"]) * (price - exact_from_str(position["entry_fill"])))


def unrealized_total(state: dict) -> int:
    """Sum of the rounded unrealized P&L of every position that has a mark; unmarked ones count as 0."""
    total = 0
    for position in state["positions"].values():
        mark = state["marks"].get(position["symbol"])
        if mark is not None:
            total += position_unrealized(position, mark["price"])
    return total


def sizing_base(state: dict, config: WalletV3Config) -> Fraction:
    """Amount (e8) the risk fraction and exposure cap are applied to: the realized balance, or equity."""
    base = state["balance_e8"] + (unrealized_total(state) if config.sizing_basis == "equity" else 0)
    return Fraction(base)


def _maintenance_margin(position: dict, notional: Fraction, config: WalletV3Config) -> Fraction:
    return notional * config.mmr


def account_summary(state: dict, config: WalletV3Config = WalletV3Config(), *, as_of_ms: int | None = None,
                    max_mark_age_ms: int | None = None) -> dict:
    """Every account quantity, separately (e8 integers), plus one entry per open position.

    ``available`` is the wallet balance minus committed margin (and margin reserved for working orders). A position
    without a mark has ``mark``/``unrealized_e8`` None and its notional and liquidation distance use the entry
    price; ``mark_stale`` is set only when ``as_of_ms`` and ``max_mark_age_ms`` are given. Pure: no clock.
    """
    positions, unrealized, maintenance, exposure, unmarked = [], 0, 0, 0, 0
    for setup_id, position in sorted(state["positions"].items()):
        side, qty = position["side"], exact_from_str(position["qty"])
        entry, lp = exact_from_str(position["entry_fill"]), exact_from_str(position["liquidation_price"])
        mark = state["marks"].get(position["symbol"])
        price = mark["price"] if mark is not None else None
        reference = Fraction(price) if price is not None else entry
        pnl = position_unrealized(position, price) if price is not None else None
        notional = qty * reference
        mm = _round(_maintenance_margin(position, notional, config))
        stale = None
        if mark is not None and as_of_ms is not None and max_mark_age_ms is not None:
            stale = as_of_ms - mark["ms"] > max_mark_age_ms
        if price is None:
            unmarked += 1
        else:
            unrealized += pnl
        maintenance += mm
        exposure += _round(notional)
        positions.append({
            "setup_id": setup_id, "symbol": position["symbol"], "side": side, "qty": position["qty"],
            "entry": position["entry_fill"], "mark": price, "mark_ms": mark["ms"] if mark is not None else None,
            "mark_stale": stale, "unrealized_e8": pnl, "margin_e8": position["margin_e8"],
            "maintenance_margin_e8": mm, "notional_e8": _round(notional), "leverage": position["leverage"],
            "liquidation_price": position["liquidation_price"],
            "liquidation_distance": exact_to_str(side * (reference - lp) / reference),
            "stop": position["stop"], "target": position["target"], "entry_ms": position["entry_ms"]})
    totals = state["totals"]
    return {"version": WALLET_VERSION, "balance_e8": state["balance_e8"], "used_margin_e8": state["used_margin_e8"],
            "reserved_e8": state["reserved_e8"],
            "available_e8": state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"],
            "maintenance_margin_e8": maintenance, "notional_exposure_e8": exposure,
            "realized_e8": totals["realized_e8"], "fees_e8": totals["fees_e8"], "funding_e8": totals["funding_e8"],
            "liquidation_e8": totals["liquidation_e8"], "slippage_e8": totals["slippage_e8"],
            "unrealized_e8": unrealized, "equity_e8": state["balance_e8"] + unrealized,
            "unmarked_positions": unmarked, "sizing_basis": config.sizing_basis, "positions": positions}


# ---------------------------------------------------------------- terms and liquidation


def liquidation_price_for(side: int, qty: Fraction, entry: Fraction, margin: Fraction, mmr: Fraction,
                          cum: Fraction = Fraction(0)) -> Fraction:
    """Isolated, one-way liquidation price of a position: LP = (WB + cum - s*Q*EP) / (Q*MMR - s*Q).

    ``margin`` is the isolated wallet balance WB (e8), ``cum`` the bracket's maintenance amount (e8); with Q = 1,
    WB = EP / L and cum = 0 this is ``costs.liquidation_price``.
    """
    return (margin + cum - side * qty * entry) / (qty * mmr - side * qty)


def _position_lp(position: dict, config: WalletV3Config) -> Fraction:
    qty, entry = exact_from_str(position["qty"]), exact_from_str(position["entry_fill"])
    return liquidation_price_for(position["side"], qty, entry, Fraction(position["margin_e8"]),
                                 config.mmr + config.taker_rate)


# ---------------------------------------------------------------- one step


def _sizing(state: dict, config: WalletV3Config, setup: dict):
    """Plan an open: ``(plan, None)`` or ``(None, reason)``. Arithmetic and order of checks are wallet-v2's."""
    terms = position_terms(setup, config) if setup.get("status") == "T" else None
    if terms is None:
        return None, ("not_tradeable" if setup.get("status") != "T" else "no_admissible_leverage")
    fill, leverage, lp = terms
    base = sizing_base(state, config)
    risk = base * config.risk_fraction
    qty = risk / abs(fill - setup["stop"])
    notional = qty * fill
    margin = notional / leverage
    fee = notional * config.taker_rate
    exposure = sum(exact_from_str(p["notional_e8"]) for p in state["positions"].values())
    reason = None
    if len(state["positions"]) >= config.max_positions:
        reason = "cap:max_positions"
    elif exposure + notional > config.max_exposure_multiple * base:
        reason = "cap:total_exposure"
    elif state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"] < margin + fee:
        reason = "insufficient_margin"
    if reason is not None:
        return None, reason
    return {"fill": fill, "leverage": leverage, "lp": lp, "qty": qty, "notional": notional, "margin": margin,
            "fee": fee}, None


def step(state: dict, events, config: WalletV3Config = WalletV3Config()) -> tuple[dict, list]:
    """Apply events in (ms, liquidation < close < funding < mark < fill < open, id) order; (new state, ledger)."""
    state = _copy_state(state)
    done, seen_fills = set(state["done"]), set(state["fill_ids"])
    ledger: list = []

    def entry(kind: str, ms: int, amount: int, event: dict | None = None, **fields) -> None:
        state["seq"] += 1
        state["balance_e8"] += amount
        totals = state["totals"]
        if kind in ("open_fee", "close_fee"):
            totals["fees_e8"] += amount
        elif kind == "pnl":
            totals["realized_e8"] += amount
        elif kind == "funding":
            totals["funding_e8"] += amount
        elif kind == "liquidation":
            totals["liquidation_e8"] += amount
        line = {"seq": state["seq"], "ms": ms, "type": kind, "amount_e8": amount,
                "balance_e8": state["balance_e8"], "assumptions": list(config.assumptions), **fields}
        if config.ledger_links:
            line.update(_links(config, event, fields.get("setup_id")))
        ledger.append(line)

    def finish(setup_id: str) -> None:
        done.add(setup_id)
        state["done"].append(setup_id)

    def slippage(reference, price: Fraction, qty: Fraction) -> None:
        state["totals"]["slippage_e8"] += _round(abs(price - reference) * qty)

    ordered = sorted(events, key=lambda ev: (ev["ms"], _ORDER[ev["type"]],
                                             ev.get("setup_id") or ev.get("symbol", ""), ev.get("fill_id", "")))
    for event in ordered:
        kind, ms = event["type"], event["ms"]
        if kind == "open":
            setup = event["setup"]
            setup_id = setup["setup_id"]
            if setup_id in done or setup_id in state["positions"]:
                continue
            base = {"setup_id": setup_id, "symbol": setup["symbol"]}
            plan, reason = _sizing(state, config, setup)
            if reason is not None:
                entry("rejected", ms, 0, event, reason=reason, **base)
                finish(setup_id)
                continue
            fee_e8 = _round(plan["fee"])
            margin_e8 = ceil(plan["margin"])
            state["used_margin_e8"] += margin_e8
            qty = plan["qty"]
            slippage(setup["p0"], plan["fill"], qty)
            state["positions"][setup_id] = {
                "symbol": setup["symbol"], "side": setup["side"], "entry_ms": setup["entry_ms"],
                "stop": setup["stop"], "target": setup["target"], "qty": exact_to_str(qty),
                "entry_fill": exact_to_str(plan["fill"]), "leverage": plan["leverage"],
                "leverage_cap": config.leverage_cap, "liquidation_rule": v2.WALLET_VERSION,
                "liquidation_price": exact_to_str(plan["lp"]), "margin_e8": margin_e8,
                "notional_e8": exact_to_str(plan["notional"])}
            entry("open_fee", ms, -fee_e8, event, leverage=plan["leverage"], qty=exact_to_str(qty),
                  margin_e8=margin_e8, **base)
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
                # Cap once at the stored, rounded-up isolated margin (see wallet-v2).
                loss = min(costs.liquidation_loss(side, fill, lp, config.model()) * qty,
                           Fraction(position["margin_e8"]))
                entry("liquidation", ms, -_round(loss), event, **base)
            else:
                exit_ref = event.get("exit_ref_price")
                exit_fill = fill if exit_ref is None else _slipped(exit_ref, side, config, False)
                if exit_ref is not None:
                    slippage(exit_ref, exit_fill, qty)
                entry("pnl", ms, _round(side * qty * (exit_fill - fill)), event, outcome=event.get("outcome"), **base)
                entry("close_fee", ms, -_round(qty * exit_fill * config.taker_rate), event, **base)
            state["used_margin_e8"] -= position["margin_e8"]
            del state["positions"][setup_id]
            finish(setup_id)
        elif kind == "funding":
            rate, mark = Fraction(str(event["rate"])), event["mark"]
            for setup_id, position in sorted(state["positions"].items()):
                if position["symbol"] != event["symbol"] or position["entry_ms"] >= ms:
                    continue
                cash = -position["side"] * exact_from_str(position["qty"]) * mark * rate
                entry("funding", ms, _round(cash), event, setup_id=setup_id, symbol=position["symbol"],
                      rate=event["rate"])
        elif kind == "mark":
            prior = state["marks"].get(event["symbol"])
            if prior is None or ms >= prior["ms"]:
                state["marks"][event["symbol"]] = {"ms": ms, "price": int(event["mark"])}
        elif kind == "fill":
            _fill(state, config, event, entry, finish, slippage, seen_fills)
        else:
            raise ValueError(f"unknown wallet event {kind!r}")
    return state, ledger


def _links(config: WalletV3Config, event: dict | None, setup_id: str | None) -> dict:
    """Run, position and source-event links of one ledger line (one position per setup, in one-way mode)."""
    if event is not None and event.get("stage_run_id") is not None:
        symbol = event.get("symbol") or (event.get("setup") or {}).get("symbol")
        source = {"stage_run_id": event["stage_run_id"], "symbol": symbol, "sequence": event.get("sequence")}
    elif event is not None:
        source = {"type": event["type"], "ms": event["ms"]}
    else:
        source = None
    return {"run_id": config.run_id, "position_id": setup_id, "source_event": source}


def _fill(state: dict, config: WalletV3Config, event: dict, entry, finish, slippage, seen_fills: set) -> None:
    """One scale-in or partial exit. Idempotent by ``fill_id``; every outcome (even a rejection) records the id."""
    ms, setup_id, fill_id = event["ms"], event["setup_id"], event["fill_id"]
    if event["direction"] not in ("increase", "reduce"):
        raise ValueError(f"unknown fill direction {event['direction']!r}")
    if fill_id in seen_fills:
        return
    seen_fills.add(fill_id)
    state["fill_ids"].append(fill_id)
    position = state["positions"].get(setup_id)
    qty = exact_from_str(event["qty"])
    symbol = position["symbol"] if position is not None else event.get("symbol")
    base = {"setup_id": setup_id, "symbol": symbol, "fill_id": fill_id}
    increase = event["direction"] == "increase"

    def reject(reason: str) -> None:
        entry("rejected", ms, 0, event, reason=reason, direction=event["direction"], qty=event["qty"], **base)

    if position is None:
        return reject("no_open_position")
    if qty <= 0:
        return reject("invalid_fill_quantity")
    side, held = position["side"], exact_from_str(position["qty"])
    taker = event.get("liquidity", "taker") != "maker"
    rate = config.taker_rate if taker else config.maker_rate
    reference = event["price"]
    price = _slipped(reference, side, config, increase) if taker else Fraction(reference)
    entry_price = exact_from_str(position["entry_fill"])
    if increase:
        notional = qty * price
        margin = notional / position["leverage"]
        fee = notional * rate
        base_amount = sizing_base(state, config)
        exposure = sum(exact_from_str(p["notional_e8"]) for p in state["positions"].values())
        if exposure + notional > config.max_exposure_multiple * base_amount:
            return reject("cap:total_exposure")
        if state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"] < margin + fee:
            return reject("insufficient_margin")
        new_qty = held + qty
        new_entry = (held * entry_price + qty * price) / new_qty
        margin_e8 = ceil(margin)
        state["used_margin_e8"] += margin_e8
        position.update({"qty": exact_to_str(new_qty), "entry_fill": exact_to_str(new_entry),
                         "margin_e8": position["margin_e8"] + margin_e8,
                         "notional_e8": exact_to_str(new_qty * new_entry)})
        position["liquidation_price"] = exact_to_str(_position_lp(position, config))
        if taker:
            slippage(reference, price, qty)
        entry("open_fee", ms, -_round(fee), event, qty=event["qty"], margin_e8=margin_e8,
              position_event="increased", liquidity="taker" if taker else "maker", **base)
        return
    if qty > held:
        return reject("over_close")
    closing = qty == held
    released = position["margin_e8"] if closing else floor(Fraction(position["margin_e8"]) * qty / held)
    realized = _round(side * qty * (price - entry_price))
    fee_e8 = _round(qty * price * rate)
    if taker:
        slippage(reference, price, qty)
    detail = {"qty": event["qty"], "margin_released_e8": released,
              "position_event": "closed" if closing else "reduced", "liquidity": "taker" if taker else "maker"}
    entry("pnl", ms, realized, event, outcome=event.get("outcome"), **detail, **base)
    entry("close_fee", ms, -fee_e8, event, **detail, **base)
    state["used_margin_e8"] -= released
    if closing:
        del state["positions"][setup_id]
        finish(setup_id)
        return
    remaining = held - qty
    position.update({"qty": exact_to_str(remaining), "margin_e8": position["margin_e8"] - released,
                     "notional_e8": exact_to_str(remaining * entry_price)})
    position["liquidation_price"] = exact_to_str(_position_lp(position, config))


def wallet_step(state: dict, events, config: WalletV3Config = WalletV3Config(), *, adverse_paths=(),
                compatibility: bool = False) -> tuple[dict, list]:
    """Portfolio composition of ordered per-symbol market events, as ``wallet.wallet_step`` but with v3 ``step``.

    Liquidation eligibility comes from an opens-only preflight (caps and margin rejections apply), then the full
    ordered step runs. A liquidation is derived from the terms a position had when it opened.
    """
    merged = sorted(events, key=lambda event: (event["ms"], event["symbol"], event["sequence"]))
    identities = [(event["stage_run_id"], event["symbol"], event["sequence"]) for event in merged]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate stage/symbol event sequence")
    if compatibility and any("compatibility_order" in event for event in merged):
        orders = [event.get("compatibility_order") for event in merged]
        if any(type(order) is not int for order in orders) or len(set(orders)) != len(orders):
            raise ValueError("compatibility_order must be present and unique on every event")
        merged.sort(key=lambda event: event["compatibility_order"])
    execution = [{key: value for key, value in event.items()
                  if not (event["type"] == "open" and key == "symbol")} for event in merged]
    opened, _ = step(state, [event for event in execution if event["type"] in _PRE_OPEN], config)
    eligible = state["positions"].keys() | opened["positions"].keys()
    execution += path_liquidations(opened["positions"], adverse_paths)
    execution = [event for event in execution
                 if event["type"] != "liquidation" or event["setup_id"] in eligible]
    return step(state, execution, config)
