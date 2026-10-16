"""Wallet reconciliation (#36): ``reconcile(initial, ledger, state) -> report``. Pure; reports, never corrects.

Works for wallet-v2 and wallet-v3 ledgers and states. Checks (every mismatch is reported, with counts per check):

- ``seq``: ledger sequence numbers are consecutive from ``first_seq`` (1 for a whole ledger);
- ``line_balance``: each line's stored balance equals the running balance (initial + amounts so far);
- ``state_balance`` / ``state_seq``: the stored state agrees with the ledger's last line;
- ``used_margin``: the state's used margin equals the sum of the open positions' margin;
- ``negative_available``: balance - used margin - reserved margin is not negative;
- ``position_quantity``: an open quantity is never negative (and an open position is never empty);
- ``reserved_margin``: reserved margin equals the sum over working orders (wallet-v3);
- ``duplicate_fill`` / ``duplicate_order_line``: a fill id or an order lifecycle step never appears twice;
- ``totals``: realized + fees + funding + liquidation totals explain the balance change (wallet-v3 states).

``first_seq`` > 1 with ``initial`` = the balance before the first line checks a slice of a ledger (one step).
"""
from __future__ import annotations

from collections import Counter

from ..benchmark.canonical import exact_from_str

RECONCILE_VERSION = "reconcile-v1"
CHECKS = ("seq", "line_balance", "state_balance", "state_seq", "used_margin", "negative_available",
          "position_quantity", "reserved_margin", "duplicate_fill", "duplicate_order_line", "totals")
_TOTAL_KINDS = {"open_fee": "fees_e8", "close_fee": "fees_e8", "pnl": "realized_e8", "funding": "funding_e8",
                "liquidation": "liquidation_e8"}


def reconcile(initial_e8: int, ledger, state: dict, *, first_seq: int = 1) -> dict:
    mismatches: list = []

    def miss(check: str, **detail) -> None:
        mismatches.append({"check": check, **detail})

    lines = list(ledger)
    balance, expected_seq = int(initial_e8), first_seq
    sums = {name: 0 for name in set(_TOTAL_KINDS.values())}
    fills, order_steps = Counter(), Counter()
    for line in lines:
        if line["seq"] != expected_seq:
            miss("seq", seq=line["seq"], expected=expected_seq)
        expected_seq = line["seq"] + 1
        balance += line["amount_e8"]
        if line["balance_e8"] != balance:
            miss("line_balance", seq=line["seq"], expected=balance, actual=line["balance_e8"])
        if line["type"] in _TOTAL_KINDS:
            sums[_TOTAL_KINDS[line["type"]]] += line["amount_e8"]
        fill_id = line.get("fill_id")
        if fill_id is not None and line["type"] in ("pnl", "open_fee", "close_fee", "rejected"):
            fills[(fill_id, line["type"])] += 1
        if str(line["type"]).startswith("order_") and line.get("order_id") is not None:
            order_steps[(line["order_id"], line["type"], line.get("fill_id"))] += 1
    for (fill_id, kind), count in fills.items():
        if count > 1:
            miss("duplicate_fill", fill_id=fill_id, type=kind, count=count)
    for (order_id, kind, fill_id), count in order_steps.items():
        # An order is accepted, cancelled or expired once; each fill step has its own fill id.
        if count > 1:
            miss("duplicate_order_line", order_id=order_id, type=kind, count=count)

    if state["balance_e8"] != balance:
        miss("state_balance", expected=balance, actual=state["balance_e8"])
    if lines and state["seq"] != lines[-1]["seq"]:
        miss("state_seq", expected=lines[-1]["seq"], actual=state["seq"])

    positions = state.get("positions", {})
    margin = sum(int(position["margin_e8"]) for position in positions.values())
    if state["used_margin_e8"] != margin:
        miss("used_margin", expected=margin, actual=state["used_margin_e8"])
    reserved = int(state.get("reserved_e8", 0))
    if state["balance_e8"] - state["used_margin_e8"] - reserved < 0:
        miss("negative_available", available=state["balance_e8"] - state["used_margin_e8"] - reserved)
    for setup_id, position in sorted(positions.items()):
        if exact_from_str(position["qty"]) <= 0:
            miss("position_quantity", setup_id=setup_id, qty=position["qty"])
    working = sum(int(order.get("reserved_e8", 0)) for order in state.get("orders", {}).values())
    if "orders" in state and reserved != working:
        miss("reserved_margin", expected=working, actual=reserved)

    totals = state.get("totals")
    if totals is not None and first_seq == 1:
        explained = int(initial_e8) + sum(int(totals[name]) for name in sums)
        if explained != state["balance_e8"]:
            miss("totals", expected=explained, actual=state["balance_e8"])
        for name, value in sums.items():
            if lines and int(totals[name]) != value:
                miss("totals", total=name, expected=value, actual=totals[name])

    counts = {check: sum(1 for item in mismatches if item["check"] == check) for check in CHECKS}
    return {"version": RECONCILE_VERSION, "ok": not mismatches, "lines": len(lines), "positions": len(positions),
            "counts": {check: count for check, count in counts.items() if count}, "mismatches": mismatches}
