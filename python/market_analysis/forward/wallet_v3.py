"""Paper wallet v3 (#36): the wallet-v2 rules plus mark-to-market, exact fills, contract tiers, limits and orders.

Pure and append-only, like ``wallet.py`` (which is NOT changed: its ledger is stored and the forward-parity runs
depend on it). Everything new is selected by the run configuration (``WalletV3Config``); with every option at its
default, ``step`` reproduces ``wallet.step`` ledger lines bit for bit (tests/test_forward_wallet_v3.py, "golden").
A material change of the configuration is a NEW run: a state of another version is refused, never converted.

Part 1 (core accounting)
- ``mark`` events carry a mark price per symbol; ``account_summary`` derives per-position unrealized P&L and the
  account equity (balance + unrealized). Sizing and caps use the realized balance unless
  ``sizing_basis == "equity"``.
- ``fill`` events: scale-in (``increase``) and partial exit (``reduce``), quantity-exact, weighted average entry,
  margin released pro rata, realized P&L on the reduced part, a fee per fill. A reduction larger than the open
  quantity is a ``rejected`` line (``over_close``); a fill id already seen is ignored.
- ``ledger_links``: every line also names the run, the position and the source event.
- ``state["totals"]``: realized P&L, fees, funding, liquidation losses and (informational) slippage.

Part 2 (execution realism and portfolio limits)
- ``contract_inputs``: per-symbol maintenance-margin tiers (rate, maintenance amount, notional bracket, max
  leverage), fee schedule, liquidation fee and filters (``contract_inputs.py``). The leverage selector and the
  liquidation price use the bracket of the position's notional; the liquidation charge uses the symbol's fee.
- ``max_open_risk_fraction`` / ``cluster_risk_fraction``: portfolio limits (total open risk, and same-direction
  positions across the six coins counted as one cluster); a breach is a named, recorded rejection.
- ``order`` / ``tape`` / ``cancel`` events: market and limit intents with a ledger lifecycle
  (``order_submitted`` ... ``order_filled``). A limit order fills only when traded price CROSSES it (never on
  touch), at the limit, with the maker fee, and only from minutes that began at or after its submission.
Amounts are integers in 10**-8 USDT (``*_e8``) rounded half-even once per ledger line, quantities exact fractions.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from math import ceil, floor

from ..benchmark import costs
from ..benchmark.canonical import exact_from_str, exact_to_str
from . import wallet as v2
from .contract_inputs import E8 as _E8, FROZEN_SYMBOLS, PLACEHOLDER_TIER, ContractInputs, Tier
from .wallet import FLAT_TIER, _round, _slipped, path_liquidations

WALLET_VERSION = "wallet-v3"
SIZING_BASES = ("balance", "equity")
MINUTE_MS = 60_000
# Owner default of #36: total open risk at most 5 % of the balance (when the limit is switched on).
DEFAULT_MAX_OPEN_RISK = Fraction(5, 100)
# PLACEHOLDER (not supplied by the owner): same-direction cluster open risk at most 3 % of the balance.
PLACEHOLDER_CLUSTER_RISK = Fraction(3, 100)
# Same relative order as wallet-v2 (liquidation < close < funding < open); the new kinds slot in around it.
_ORDER = {"liquidation": 0, "close": 1, "funding": 2, "cancel": 3, "tape": 4, "mark": 5, "fill": 6,
          "order": 7, "open": 8}
_PRE_OPEN = ("open", "order", "tape", "cancel")
ORDER_KINDS = ("order_submitted", "order_accepted", "order_rejected", "order_cancelled", "order_expired",
               "order_filled", "order_partially_filled", "order_ambiguous")
# Ledger kinds reserved for the exit-management work of #221 (trailing, break-even): nothing emits them yet.
RESERVED_KINDS = ("stop_moved", "target_moved")
LEDGER_KINDS = ("open_fee", "pnl", "close_fee", "funding", "liquidation", "rejected", *ORDER_KINDS, *RESERVED_KINDS)


@dataclass(frozen=True)
class WalletV3Config(v2.WalletConfig):
    maker_rate: Fraction = costs.COST_MODEL_V1.maker_rate
    sizing_basis: str = "balance"
    ledger_links: bool = False
    run_id: str = ""
    contract_inputs: ContractInputs | None = None
    max_open_risk_fraction: Fraction | None = None
    cluster_risk_fraction: Fraction | None = None

    def __post_init__(self):
        if self.sizing_basis not in SIZING_BASES:
            raise ValueError(f"sizing_basis must be one of {SIZING_BASES}")
        for name in ("max_open_risk_fraction", "cluster_risk_fraction"):
            value = getattr(self, name)
            if value is not None and not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.contract_inputs is not None and self.assumptions == (FLAT_TIER,):
            # Some tier values are placeholders; a line in an owner-supplied tier says so (see ``labels``).
            object.__setattr__(self, "assumptions", (PLACEHOLDER_TIER,))

    def to_record(self) -> dict:
        record = super().to_record()
        record.update({
            "version": WALLET_VERSION, "wallet_version": WALLET_VERSION,
            "maker_rate": exact_to_str(self.maker_rate), "sizing_basis": self.sizing_basis,
            "ledger_links": self.ledger_links, "run_id": self.run_id,
            "contract_inputs": self.contract_inputs is not None,
            "contract_inputs_version": self.contract_inputs.version if self.contract_inputs else None,
            "contract_inputs_hash": self.contract_inputs.sha256 if self.contract_inputs else None,
            "max_open_risk_fraction": None if self.max_open_risk_fraction is None
            else exact_to_str(self.max_open_risk_fraction),
            "cluster_risk_fraction": None if self.cluster_risk_fraction is None
            else exact_to_str(self.cluster_risk_fraction)})
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


# ---------------------------------------------------------------- per-symbol inputs and brackets


def _inputs(config: WalletV3Config, symbol: str):
    return config.contract_inputs.symbol(symbol) if config.contract_inputs is not None else None


def taker_rate_of(config: WalletV3Config, symbol: str) -> Fraction:
    inputs = _inputs(config, symbol)
    return inputs.taker_rate if inputs is not None else config.taker_rate


def maker_rate_of(config: WalletV3Config, symbol: str) -> Fraction:
    inputs = _inputs(config, symbol)
    return inputs.maker_rate if inputs is not None else config.maker_rate


def liquidation_model(config: WalletV3Config, symbol: str) -> costs.CostModel:
    """The cost model of a liquidation: the symbol's own liquidation fee when contract inputs are configured."""
    inputs = _inputs(config, symbol)
    model = config.model()
    return model if inputs is None else replace(model, liquidation_fee_rate=inputs.liquidation_fee_rate)


@dataclass(frozen=True)
class Bracket:
    """Maintenance terms of one position: rate (without the closing-fee reserve), maintenance amount (e8),
    maximum leverage and the ledger assumption code. ``tier`` is None for the flat tier."""
    mmr: Fraction
    cum_e8: Fraction
    max_leverage: int
    label: str
    tier: Tier | None = None


def bracket_for(config: WalletV3Config, symbol: str, notional_e8) -> Bracket | None:
    """The bracket of a position notional; None above the last tier (unavailable, never extrapolated)."""
    inputs = _inputs(config, symbol)
    if inputs is None:
        return Bracket(config.mmr, Fraction(0), config.leverage_cap, FLAT_TIER)
    tier = inputs.tier_for(notional_e8)
    return None if tier is None else Bracket(tier.mmr, tier.cum * _E8, tier.max_leverage, tier.label, tier)


def _last_bracket(config: WalletV3Config, symbol: str) -> Bracket:
    tier = _inputs(config, symbol).tiers[-1]
    return Bracket(tier.mmr, tier.cum * _E8, tier.max_leverage, tier.label, tier)


def liquidation_price_for(side: int, qty: Fraction, entry: Fraction, margin: Fraction, mmr: Fraction,
                          cum: Fraction = Fraction(0)) -> Fraction:
    """Isolated, one-way liquidation price of a position: LP = (WB + cum - s*Q*EP) / (Q*MMR - s*Q).

    ``margin`` is the isolated wallet balance WB (e8), ``cum`` the bracket's maintenance amount (e8); with Q = 1,
    WB = EP / L and cum = 0 this is ``costs.liquidation_price``. The distance of the price from the entry is
    ``(WB + cum - Q*EP*MMR) / (Q * (1 - s*MMR))`` for both sides.
    """
    return (margin + cum - side * qty * entry) / (qty * mmr - side * qty)


def tiered_terms(side: int, fill: Fraction, qty: Fraction, stop_distance: Fraction, bracket: Bracket,
                 taker_rate: Fraction, config: WalletV3Config):
    """(leverage, liquidation price) of a position inside ``bracket``, or None when even 1x is too close.

    The largest integer leverage L (at most the config cap and the bracket's maximum) whose liquidation price is at
    least ``liquidation_buffer * stop_distance`` from the entry. With WB = N / L (N the notional), the distance
    condition is ``1/L >= MMR + (need*Q*(1 - s*MMR) - cum) / N`` (MMR including the closing-fee reserve).
    """
    notional = qty * fill
    mmr = bracket.mmr + taker_rate
    need = config.liquidation_buffer * Fraction(stop_distance)
    threshold = mmr + (need * qty * (1 - side * mmr) - bracket.cum_e8) / notional
    cap = min(config.leverage_cap, bracket.max_leverage)
    leverage = cap if threshold <= 0 else min(cap, floor(1 / threshold))
    if leverage < 1:
        return None
    lp = liquidation_price_for(side, qty, fill, notional / leverage, mmr, bracket.cum_e8)
    if side * (fill - lp) < need:
        raise ArithmeticError("closed-form leverage does not satisfy the liquidation buffer")
    return leverage, lp


def _flat_terms(side: int, fill: Fraction, stop: int, config: WalletV3Config):
    """wallet-v2's leverage rule (``wallet.position_terms``) for an explicit fill price."""
    model = replace(config.model(), mmr=config.mmr + config.taker_rate)
    leverage = costs.max_admissible_leverage(side, fill, abs(fill - stop), model)
    if leverage is None:
        return None
    return leverage, costs.liquidation_price(side, fill, leverage, model.mmr)


def _position_lp(position: dict, config: WalletV3Config) -> Fraction:
    """Liquidation price from the position's present quantity, entry, isolated margin and bracket."""
    qty, entry = exact_from_str(position["qty"]), exact_from_str(position["entry_fill"])
    symbol = position["symbol"]
    bracket = bracket_for(config, symbol, qty * entry) or _last_bracket(config, symbol)
    return liquidation_price_for(position["side"], qty, entry, Fraction(position["margin_e8"]),
                                 bracket.mmr + taker_rate_of(config, symbol), bracket.cum_e8)


def _apply_bracket(position: dict, bracket: Bracket) -> None:
    if bracket.tier is not None:
        position.update({"tier_label": bracket.label, "mmr": exact_to_str(bracket.mmr),
                         "cum_e8": exact_to_str(bracket.cum_e8)})


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
    """``notional * mmr - cum`` of the bracket of that notional (the last tier above the table)."""
    symbol = position["symbol"]
    bracket = bracket_for(config, symbol, notional)
    if bracket is None:
        bracket = _last_bracket(config, symbol)
    return max(Fraction(0), notional * bracket.mmr - bracket.cum_e8)


def account_summary(state: dict, config: WalletV3Config = WalletV3Config(), *, as_of_ms: int | None = None,
                    max_mark_age_ms: int | None = None) -> dict:
    """Every account quantity, separately (e8 integers), plus one entry per open position and working order.

    ``available`` is the wallet balance minus committed margin and margin reserved for working orders. A position
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
            "stop": position["stop"], "target": position["target"], "entry_ms": position["entry_ms"],
            "tier_label": position.get("tier_label", FLAT_TIER)})
    working = [{"order_id": order["order_id"], "setup_id": order["setup_id"], "symbol": order["symbol"],
                "side": order["side"], "order_type": order["order_type"], "limit_price": order["limit_price"],
                "qty": order["qty"], "filled": order["filled"], "expires_ms": order["expires_ms"],
                "reserved_e8": order["reserved_e8"]} for order in _working(state)]
    totals = state["totals"]
    return {"version": WALLET_VERSION, "balance_e8": state["balance_e8"], "used_margin_e8": state["used_margin_e8"],
            "reserved_e8": state["reserved_e8"],
            "available_e8": state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"],
            "maintenance_margin_e8": maintenance, "notional_exposure_e8": exposure,
            "realized_e8": totals["realized_e8"], "fees_e8": totals["fees_e8"], "funding_e8": totals["funding_e8"],
            "liquidation_e8": totals["liquidation_e8"], "slippage_e8": totals["slippage_e8"],
            "unrealized_e8": unrealized, "equity_e8": state["balance_e8"] + unrealized,
            "unmarked_positions": unmarked, "sizing_basis": config.sizing_basis, "positions": positions,
            "working_orders": working}


# ---------------------------------------------------------------- admission (sizing, caps, margin)


def _risk_of(qty: Fraction, price: Fraction, stop) -> Fraction:
    return qty * abs(price - stop)


def _working(state: dict) -> list:
    return [order for _, order in sorted(state["orders"].items()) if order["status"] == "working"]


def _remaining(order: dict) -> Fraction:
    return exact_from_str(order["qty"]) - exact_from_str(order["filled"])


def _commitments(state: dict):
    """Open positions plus working orders: (count, exposure e8, [(symbol, side, open risk e8)]).

    A working order counts as one position slot until it has produced a fill (after that its position does)."""
    count, exposure, risks = len(state["positions"]), Fraction(0), []
    for position in state["positions"].values():
        exposure += exact_from_str(position["notional_e8"])
        risks.append((position["symbol"], position["side"],
                      _risk_of(exact_from_str(position["qty"]), exact_from_str(position["entry_fill"]),
                               position["stop"])))
    for order in _working(state):
        remaining, price = _remaining(order), Fraction(order["limit_price"])
        if exact_from_str(order["filled"]) == 0:
            count += 1
        exposure += remaining * price
        risks.append((order["symbol"], order["side"], _risk_of(remaining, price, order["stop"])))
    return count, exposure, risks


def _cap_reason(state: dict, config: WalletV3Config, base: Fraction, symbol: str, side: int, notional: Fraction,
                new_risk: Fraction, margin: Fraction, fee: Fraction, *, new_position: bool) -> str | None:
    """The first admission rule a new position, order or scale-in breaks (wallet-v2's order, then the limits)."""
    count, exposure, risks = _commitments(state)
    if new_position and count >= config.max_positions:
        return "cap:max_positions"
    if exposure + notional > config.max_exposure_multiple * base:
        return "cap:total_exposure"
    if config.max_open_risk_fraction is not None:
        if sum(risk for _, _, risk in risks) + new_risk > config.max_open_risk_fraction * base:
            return "cap:total_open_risk"
    if config.cluster_risk_fraction is not None and symbol in FROZEN_SYMBOLS:
        cluster = sum(risk for name, direction, risk in risks if name in FROZEN_SYMBOLS and direction == side)
        if cluster + new_risk > config.cluster_risk_fraction * base:
            return "cap:cluster_risk"
    if state["balance_e8"] - state["used_margin_e8"] - state["reserved_e8"] < margin + fee:
        return "insufficient_margin"
    return None


def _plan(state: dict, config: WalletV3Config, setup: dict, *, limit_price: int | None = None):
    """Plan a new position (market) or order (limit): ``(plan, None)`` or ``(None, reason)``.

    Without contract inputs the arithmetic and the order of checks are wallet-v2's. With them the quantity is
    sized first (risk / stop distance, floored to the step size), the bracket of that notional is looked up, and
    the leverage selector runs inside it."""
    if setup.get("status") != "T":
        return None, "not_tradeable"
    symbol, side, stop = setup["symbol"], setup["side"], setup["stop"]
    try:
        inputs = _inputs(config, symbol)
    except ValueError:
        return None, "no_contract_inputs"
    taker = taker_rate_of(config, symbol)
    rate = maker_rate_of(config, symbol) if limit_price is not None else taker
    fill = Fraction(limit_price) if limit_price is not None else _slipped(setup["p0"], side, config, True)
    base = sizing_base(state, config)
    qty = base * config.risk_fraction / abs(fill - stop)
    bracket = None
    if inputs is None:
        terms = _flat_terms(side, fill, stop, config)
        if terms is None:
            return None, "no_admissible_leverage"
        leverage, lp = terms
    else:
        if inputs.step_size is not None:
            qty = Fraction(floor(qty / inputs.step_size)) * inputs.step_size
            if qty <= 0:
                return None, "below_step_size"
        if inputs.min_notional is not None and qty * fill < inputs.min_notional * _E8:
            return None, "below_min_notional"
        bracket = bracket_for(config, symbol, qty * fill)
        if bracket is None:
            return None, "notional_above_tier_table"
        terms = tiered_terms(side, fill, qty, abs(fill - stop), bracket, taker, config)
        if terms is None:
            return None, "no_admissible_leverage"
        leverage, lp = terms
    notional = qty * fill
    margin = notional / leverage
    fee = notional * rate
    reason = _cap_reason(state, config, base, symbol, side, notional, _risk_of(qty, fill, stop), margin, fee,
                         new_position=True)
    if reason is not None:
        return None, reason
    return {"fill": fill, "leverage": leverage, "lp": lp, "qty": qty, "notional": notional, "margin": margin,
            "fee": fee, "bracket": bracket}, None


def _new_position(setup: dict, plan: dict, config: WalletV3Config, *, entry_ms: int, qty: Fraction,
                  margin_e8: int, entry_fill: Fraction, lp: Fraction) -> dict:
    position = {
        "symbol": setup["symbol"], "side": setup["side"], "entry_ms": entry_ms,
        "stop": setup["stop"], "target": setup["target"], "qty": exact_to_str(qty),
        "entry_fill": exact_to_str(entry_fill), "leverage": plan["leverage"],
        "leverage_cap": config.leverage_cap,
        "liquidation_rule": v2.WALLET_VERSION if plan["bracket"] is None else "wallet-v3:tiered",
        "liquidation_price": exact_to_str(lp), "margin_e8": margin_e8, "notional_e8": exact_to_str(qty * entry_fill)}
    if plan["bracket"] is not None:
        _apply_bracket(position, plan["bracket"])
    return position


# ---------------------------------------------------------------- one step


@dataclass
class _Ctx:
    state: dict
    config: WalletV3Config
    entry: object
    finish: object
    slippage: object
    done: set
    seen_fills: set


def step(state: dict, events, config: WalletV3Config = WalletV3Config()) -> tuple[dict, list]:
    """Apply events in (ms, liquidation < close < funding < cancel < tape < mark < fill < order < open) order."""
    state = _copy_state(state)
    done, seen_fills = set(state["done"]), set(state["fill_ids"])
    ledger: list = []
    inputs_hash = config.contract_inputs.sha256 if config.contract_inputs is not None else None

    def labels(setup_id: str | None, label: str | None) -> list:
        if label is not None:
            return [label]
        position = state["positions"].get(setup_id) if setup_id is not None else None
        if position is not None and "tier_label" in position:
            return [position["tier_label"]]
        return list(config.assumptions)

    def entry(kind: str, ms: int, amount: int, event: dict | None = None, label: str | None = None,
              **fields) -> None:
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
                "balance_e8": state["balance_e8"], "assumptions": labels(fields.get("setup_id"), label), **fields}
        if inputs_hash is not None:
            line["contract_inputs_hash"] = inputs_hash
        if config.ledger_links:
            line.update(_links(config, event, fields.get("setup_id")))
        ledger.append(line)

    def finish(setup_id: str) -> None:
        done.add(setup_id)
        state["done"].append(setup_id)

    def slippage(reference, price: Fraction, qty: Fraction) -> None:
        state["totals"]["slippage_e8"] += _round(abs(price - reference) * qty)

    ctx = _Ctx(state, config, entry, finish, slippage, done, seen_fills)
    ordered = sorted(events, key=lambda ev: (ev["ms"], _ORDER[ev["type"]],
                                             ev.get("setup_id") or ev.get("symbol", "") or ev.get("order_id", ""),
                                             ev.get("fill_id", "")))
    for event in ordered:
        kind, ms = event["type"], event["ms"]
        _expire(ctx, ms)
        if kind == "open":
            setup = event["setup"]
            setup_id = setup["setup_id"]
            if setup_id in done or setup_id in state["positions"] or _has_order(state, setup_id):
                continue
            base = {"setup_id": setup_id, "symbol": setup["symbol"]}
            plan, reason = _plan(state, config, setup)
            if reason is not None:
                entry("rejected", ms, 0, event, reason=reason, **base)
                finish(setup_id)
                continue
            _open_position(ctx, event, setup, plan, ms)
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
                loss = min(costs.liquidation_loss(side, fill, lp, liquidation_model(config, position["symbol"])) * qty,
                           Fraction(position["margin_e8"]))
                entry("liquidation", ms, -_round(loss), event, **base)
            else:
                exit_ref = event.get("exit_ref_price")
                exit_fill = fill if exit_ref is None else _slipped(exit_ref, side, config, False)
                if exit_ref is not None:
                    slippage(exit_ref, exit_fill, qty)
                entry("pnl", ms, _round(side * qty * (exit_fill - fill)), event, outcome=event.get("outcome"), **base)
                entry("close_fee", ms, -_round(qty * exit_fill * taker_rate_of(config, position["symbol"])), event,
                      **base)
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
            _fill(ctx, event)
        elif kind == "order":
            _order(ctx, event)
        elif kind == "tape":
            _tape(ctx, event)
        elif kind == "cancel":
            _cancel(ctx, event)
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


def _has_order(state: dict, setup_id: str) -> bool:
    return any(order["setup_id"] == setup_id for order in state["orders"].values())


def _open_position(ctx: _Ctx, event: dict, setup: dict, plan: dict, ms: int, **extra) -> None:
    """Commit a planned market open: margin, position, and the opening fee line (v2's line plus ``extra``)."""
    state, config = ctx.state, ctx.config
    fee_e8 = _round(plan["fee"])
    margin_e8 = ceil(plan["margin"])
    state["used_margin_e8"] += margin_e8
    qty = plan["qty"]
    ctx.slippage(setup["p0"], plan["fill"], qty)
    state["positions"][setup["setup_id"]] = _new_position(setup, plan, config, entry_ms=setup["entry_ms"], qty=qty,
                                                          margin_e8=margin_e8, entry_fill=plan["fill"], lp=plan["lp"])
    ctx.entry("open_fee", ms, -fee_e8, event, leverage=plan["leverage"], qty=exact_to_str(qty), margin_e8=margin_e8,
              setup_id=setup["setup_id"], symbol=setup["symbol"], **extra)


# ---------------------------------------------------------------- fills (scale-in, partial exit)


def _fill(ctx: _Ctx, event: dict) -> None:
    """One scale-in or partial exit. Idempotent by ``fill_id``; every outcome (even a rejection) records the id."""
    state, config, entry = ctx.state, ctx.config, ctx.entry
    ms, setup_id, fill_id = event["ms"], event["setup_id"], event["fill_id"]
    if event["direction"] not in ("increase", "reduce"):
        raise ValueError(f"unknown fill direction {event['direction']!r}")
    if fill_id in ctx.seen_fills:
        return
    ctx.seen_fills.add(fill_id)
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
    rate = taker_rate_of(config, symbol) if taker else maker_rate_of(config, symbol)
    reference = event["price"]
    price = _slipped(reference, side, config, increase) if taker else Fraction(reference)
    entry_price = exact_from_str(position["entry_fill"])
    liquidity = "taker" if taker else "maker"
    if increase:
        notional = qty * price
        margin = notional / position["leverage"]
        fee = notional * rate
        new_qty = held + qty
        new_entry = (held * entry_price + qty * price) / new_qty
        bracket = bracket_for(config, symbol, new_qty * new_entry)
        if bracket is None:
            return reject("notional_above_tier_table")
        if position["leverage"] > min(config.leverage_cap, bracket.max_leverage):
            return reject("leverage_above_tier_max")
        reason = _cap_reason(state, config, sizing_base(state, config), symbol, side, notional,
                             _risk_of(qty, price, position["stop"]), margin, fee, new_position=False)
        if reason is not None:
            return reject(reason)
        margin_e8 = ceil(margin)
        state["used_margin_e8"] += margin_e8
        position.update({"qty": exact_to_str(new_qty), "entry_fill": exact_to_str(new_entry),
                         "margin_e8": position["margin_e8"] + margin_e8,
                         "notional_e8": exact_to_str(new_qty * new_entry)})
        _apply_bracket(position, bracket)
        position["liquidation_price"] = exact_to_str(_position_lp(position, config))
        if taker:
            ctx.slippage(reference, price, qty)
        entry("open_fee", ms, -_round(fee), event, qty=event["qty"], margin_e8=margin_e8,
              position_event="increased", liquidity=liquidity, **base)
        return
    if qty > held:
        return reject("over_close")
    closing = qty == held
    released = position["margin_e8"] if closing else floor(Fraction(position["margin_e8"]) * qty / held)
    realized = _round(side * qty * (price - entry_price))
    fee_e8 = _round(qty * price * rate)
    if taker:
        ctx.slippage(reference, price, qty)
    detail = {"qty": event["qty"], "margin_released_e8": released,
              "position_event": "closed" if closing else "reduced", "liquidity": liquidity}
    entry("pnl", ms, realized, event, outcome=event.get("outcome"), **detail, **base)
    entry("close_fee", ms, -fee_e8, event, **detail, **base)
    state["used_margin_e8"] -= released
    if closing:
        del state["positions"][setup_id]
        ctx.finish(setup_id)
        return
    remaining = held - qty
    position.update({"qty": exact_to_str(remaining), "margin_e8": position["margin_e8"] - released,
                     "notional_e8": exact_to_str(remaining * entry_price)})
    bracket = bracket_for(config, symbol, remaining * entry_price)
    if bracket is not None:
        _apply_bracket(position, bracket)
    position["liquidation_price"] = exact_to_str(_position_lp(position, config))


# ---------------------------------------------------------------- orders


def _order_line(ctx: _Ctx, kind: str, ms: int, event: dict | None, order: dict, label: str | None = None,
                **fields) -> None:
    ctx.entry(kind, ms, 0, event, label, order_id=order["order_id"], setup_id=order["setup_id"],
              symbol=order["symbol"], order_type=order["order_type"], **fields)


def _release(ctx: _Ctx, order: dict, amount: int) -> None:
    amount = min(amount, order["reserved_e8"])
    order["reserved_e8"] -= amount
    ctx.state["reserved_e8"] -= amount


def _expire(ctx: _Ctx, ms: int) -> None:
    """Expire working orders whose expiry precedes this event (a tape stamped at the expiry may still fill them)."""
    for order in _working(ctx.state):
        if order["expires_ms"] is not None and order["expires_ms"] < ms:
            order["status"] = "expired"
            _release(ctx, order, order["reserved_e8"])
            _order_line(ctx, "order_expired", order["expires_ms"], None, order, order["label"],
                        remaining=exact_to_str(_remaining(order)))


def _order(ctx: _Ctx, event: dict) -> None:
    """Submit a market or limit intent for a tradeable setup; the lifecycle is in the ledger. Idempotent by id."""
    state, config = ctx.state, ctx.config
    order_id, ms, setup = event["order_id"], event["ms"], event["setup"]
    if order_id in state["orders"]:
        return
    otype, limit = event["order_type"], event.get("limit_price")
    order = {"order_id": order_id, "setup_id": setup["setup_id"], "symbol": setup["symbol"], "side": setup["side"],
             "order_type": otype, "status": "rejected", "submit_ms": ms, "expires_ms": event.get("expires_ms"),
             "limit_price": limit, "qty": "0", "filled": "0", "fills": 0, "reserved_e8": 0, "leverage": None,
             "stop": setup["stop"], "setup": setup, "label": None, "last_tape_ms": None, "ambiguous": False}
    state["orders"][order_id] = order
    _order_line(ctx, "order_submitted", ms, event, order, limit_price=limit, expires_ms=order["expires_ms"])

    def reject(reason: str) -> None:
        _order_line(ctx, "order_rejected", ms, event, order, reason=reason)

    if otype not in ("market", "limit"):
        return reject("invalid_order_type")
    setup_id = setup["setup_id"]
    if (setup_id in ctx.done or setup_id in state["positions"]
            or any(o["setup_id"] == setup_id and o["order_id"] != order_id and o["status"] in ("working", "filled")
                   for o in state["orders"].values())):
        return reject("setup_already_used")
    if otype == "limit":
        if not isinstance(limit, int) or limit <= 0:
            return reject("invalid_limit_price")
        # Only passive limits are supported: a buy below, a sell above the decision price. A marketable limit
        # (or one exactly at the decision price) would need a quote the wallet does not have.
        if not (limit < setup["p0"] if setup["side"] == 1 else limit > setup["p0"]):
            return reject("limit_not_passive")
        try:
            inputs = _inputs(config, setup["symbol"])
        except ValueError:
            return reject("no_contract_inputs")
        if inputs is not None and inputs.tick_size is not None and Fraction(limit) % (inputs.tick_size * _E8) != 0:
            return reject("price_off_tick")
        if event.get("expires_ms") is not None and event["expires_ms"] <= ms:
            return reject("invalid_expiry")
    plan, reason = _plan(state, config, setup, limit_price=limit if otype == "limit" else None)
    if reason is not None:
        return reject(reason)
    label = plan["bracket"].label if plan["bracket"] is not None else None
    order.update({"qty": exact_to_str(plan["qty"]), "leverage": plan["leverage"], "label": label})
    _order_line(ctx, "order_accepted", ms, event, order, label, qty=order["qty"], leverage=plan["leverage"],
                limit_price=limit)
    if otype == "limit":
        reserved = ceil(plan["margin"] + plan["fee"])
        order.update({"status": "working", "reserved_e8": reserved})
        state["reserved_e8"] += reserved
        return
    order.update({"status": "filled", "filled": order["qty"], "fills": 1})
    fill_id = f"{order_id}:1"
    _order_line(ctx, "order_filled", ms, event, order, label, fill_id=fill_id, qty=order["qty"],
                price=exact_to_str(plan["fill"]), remaining="0", liquidity="taker")
    _open_position(ctx, event, setup, plan, ms, fill_id=fill_id, order_id=order_id, liquidity="taker")


def _cancel(ctx: _Ctx, event: dict) -> None:
    order = ctx.state["orders"].get(event["order_id"])
    if order is None or order["status"] != "working":
        return
    order["status"] = "cancelled"
    _release(ctx, order, order["reserved_e8"])
    _order_line(ctx, "order_cancelled", event["ms"], event, order, order["label"],
                remaining=exact_to_str(_remaining(order)))


def crossed(side: int, limit: int, low: int, high: int) -> bool:
    """A buy limit fills only when the traded price went BELOW it, a sell only ABOVE it. Touching is not enough."""
    return low < limit if side == 1 else high > limit


def _tape(ctx: _Ctx, event: dict) -> None:
    """One completed minute of trades (stamped at its close) against the working limit orders of the symbol.

    Fill rule (conservative; the ambiguity is marked, never resolved in the order's favour):
    - the minute must have begun at or after the order's submission (no data from before the decision);
    - the price must cross the limit strictly (``low < limit`` for a buy, ``high > limit`` for a sell);
    - with ``through_qty`` (quantity traded through the limit, e.g. from aggTrades) the fill is
      ``min(remaining, through_qty)``, possibly partial, and the orders of the symbol share that quantity in
      order-id order; with bars only, the whole remainder fills only when the minute CLOSED strictly beyond the
      limit, otherwise a single ``order_ambiguous`` line is written and nothing fills (a bar cannot tell how much
      traded through the limit);
    - fills happen at the limit price with the maker fee; each minute is considered once per order (replay-safe).
    """
    state, ms = ctx.state, event["ms"]
    low, high, close = event["low"], event["high"], event["close"]
    through = None if event.get("through_qty") is None else exact_from_str(event["through_qty"])
    for order in _working(state):
        if order["symbol"] != event["symbol"] or order["order_type"] != "limit":
            continue
        if order["last_tape_ms"] is not None and ms <= order["last_tape_ms"]:
            continue
        order["last_tape_ms"] = ms
        if ms - MINUTE_MS < order["submit_ms"]:
            continue
        side, limit = order["side"], order["limit_price"]
        if not crossed(side, limit, low, high):
            continue
        remaining = _remaining(order)
        if through is not None:
            fill_qty = min(remaining, through) if through > 0 else Fraction(0)
        else:
            beyond = close < limit if side == 1 else close > limit
            fill_qty = remaining if beyond else Fraction(0)
        if fill_qty <= 0:
            if not order["ambiguous"]:
                order["ambiguous"] = True
                _order_line(ctx, "order_ambiguous", ms, event, order, order["label"],
                            reason="crossed_without_fill_evidence", remaining=exact_to_str(remaining))
            continue
        if through is not None:
            through -= fill_qty
        _limit_fill(ctx, event, order, fill_qty, ms)


def _limit_fill(ctx: _Ctx, event: dict, order: dict, qty: Fraction, ms: int) -> None:
    state, config = ctx.state, ctx.config
    symbol, side, setup_id = order["symbol"], order["side"], order["setup_id"]
    price = Fraction(order["limit_price"])
    notional = qty * price
    fee_e8 = _round(notional * maker_rate_of(config, symbol))
    margin_e8 = ceil(notional / order["leverage"])
    fills = order["fills"] + 1
    fill_id = f"{order['order_id']}:{fills}"
    filled = exact_from_str(order["filled"]) + qty
    total = exact_from_str(order["qty"])
    last = filled == total
    # The minute began at ms - 60 s; the position's clock starts there so that minute's mark extremes are included
    # in the liquidation scan (conservative: they may precede the fill).
    minute_start = ms - MINUTE_MS
    position = state["positions"].get(setup_id)
    state["used_margin_e8"] += margin_e8
    if position is None:
        bracket = (bracket_for(config, symbol, notional) or _last_bracket(config, symbol)) \
            if config.contract_inputs is not None else None
        position = _new_position(order["setup"], {"leverage": order["leverage"], "bracket": bracket}, config,
                                 entry_ms=minute_start, qty=qty, margin_e8=margin_e8, entry_fill=price,
                                 lp=Fraction(0))
        state["positions"][setup_id] = position
        position_event = "opened"
    else:
        new_qty = exact_from_str(position["qty"]) + qty
        position.update({"qty": exact_to_str(new_qty), "margin_e8": position["margin_e8"] + margin_e8,
                         "notional_e8": exact_to_str(new_qty * price)})
        position_event = "increased"
    bracket = bracket_for(config, symbol, exact_from_str(position["qty"]) * price)
    if bracket is not None:
        _apply_bracket(position, bracket)
    position["liquidation_price"] = exact_to_str(_position_lp(position, config))
    _release(ctx, order, order["reserved_e8"] if last else margin_e8 + fee_e8)
    order.update({"filled": exact_to_str(filled), "fills": fills, "status": "filled" if last else "working"})
    ambiguity = {}
    if (side == 1 and event["low"] <= order["stop"]) or (side == -1 and event["high"] >= order["stop"]):
        # The same minute reached the stop: the order of the fill and the stop is unknowable from a bar.
        ambiguity = {"ambiguity": "stop_touched_in_fill_minute"}
    _order_line(ctx, "order_filled" if last else "order_partially_filled", ms, event, order, order["label"],
                fill_id=fill_id, qty=exact_to_str(qty), price=exact_to_str(price),
                remaining=exact_to_str(total - filled), liquidity="maker", **ambiguity)
    ctx.entry("open_fee", ms, -fee_e8, event, order["label"], setup_id=setup_id, symbol=symbol,
              order_id=order["order_id"], fill_id=fill_id, qty=exact_to_str(qty), margin_e8=margin_e8,
              position_event=position_event, liquidity="maker", leverage=order["leverage"])


def wallet_step(state: dict, events, config: WalletV3Config = WalletV3Config(), *, adverse_paths=(),
                compatibility: bool = False) -> tuple[dict, list]:
    """Portfolio composition of ordered per-symbol market events, as ``wallet.wallet_step`` but with v3 ``step``.

    Liquidation eligibility comes from a preflight of the position-creating events (caps, margin and fills apply),
    then the full ordered step runs. A liquidation is derived from the terms a position had when it opened.
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
                  if not (event["type"] in ("open", "order") and key == "symbol")} for event in merged]
    opened, _ = step(state, [event for event in execution if event["type"] in _PRE_OPEN], config)
    eligible = state["positions"].keys() | opened["positions"].keys()
    execution += path_liquidations(opened["positions"], adverse_paths)
    execution = [event for event in execution
                 if event["type"] != "liquidation" or event["setup_id"] in eligible]
    return step(state, execution, config)
