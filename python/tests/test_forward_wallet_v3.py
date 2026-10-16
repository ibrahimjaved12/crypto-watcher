"""wallet-v3 (#36 Part 1): golden equivalence with wallet-v2, mark-to-market, exact fills, reconciliation, API.

Small synthetic inputs only (no sigma warm-up); the whole module runs in well under a second.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, replace
from fractions import Fraction
from math import ceil
import unittest
from unittest.mock import patch

from market_analysis.benchmark.canonical import exact_from_str
from market_analysis.forward import wallet as v2
from market_analysis.forward import wallet_v3 as v3
from market_analysis.forward.signals import Signal
from market_analysis.forward.wallet_reconcile import reconcile

from test_forward_split import engine, rows, setup as split_setup
from test_forward_wallet import MINUTE, P0, START, setup

STAGE = "00000000-0000-0000-0000-000000000001"
V2_STATE_KEYS = ("balance_e8", "used_margin_e8", "seq", "positions", "done")


def v3_of(config: v2.WalletConfig, **options) -> v3.WalletV3Config:
    return v3.WalletV3Config(**{f.name: getattr(config, f.name) for f in fields(v2.WalletConfig)}, **options)


def opens(*setups):
    return [{"type": "open", "ms": s["entry_ms"], "setup": s} for s in setups]


def mark(ms, price, symbol="BTCUSDT"):
    return {"type": "mark", "ms": ms, "symbol": symbol, "mark": price}


def fill(fill_id, direction, qty, price, ms, setup_id="s1", **extra):
    return {"type": "fill", "ms": ms, "setup_id": setup_id, "fill_id": fill_id, "direction": direction,
            "qty": qty, "price": price, **extra}


class GoldenEquivalenceTests(unittest.TestCase):
    """With every v3 option at its default the ledger is wallet-v2's, line for line and bit for bit."""

    def same(self, events, config=v2.WalletConfig(), start=None):
        config3 = v3_of(config)
        old_state, old_ledger = v2.step(start or v2.initial_state(config), events, config)
        new_state, new_ledger = v3.step(v3.initial_state(config3), events, config3)
        self.assertEqual(new_ledger, old_ledger)
        for key in V2_STATE_KEYS:
            self.assertEqual(new_state[key], old_state[key], key)
        self.assertEqual(new_state["version"], "wallet-v3")
        return old_ledger

    def test_open_close_funding_and_idempotent_replay(self):
        events = [*opens(setup("long", 1), setup("short", -1)),
                  {"type": "funding", "ms": START + 60 * MINUTE, "symbol": "BTCUSDT", "rate": "0.0001", "mark": P0},
                  {"type": "close", "ms": START + 90 * MINUTE, "setup_id": "long", "outcome": "T",
                   "exit_ref_price": P0 + 6 * 10 ** 8},
                  {"type": "close", "ms": START + 91 * MINUTE, "setup_id": "short", "outcome": "S",
                   "exit_ref_price": P0 + 2 * 10 ** 8},
                  {"type": "close", "ms": START + 92 * MINUTE, "setup_id": "short", "outcome": "S"}]
        ledger = self.same(events)
        self.assertEqual({line["type"] for line in ledger}, {"open_fee", "pnl", "close_fee", "funding"})

    def test_caps_margin_and_not_tradeable_rejections(self):
        self.same(opens(setup("a"), setup("b", entry_ms=START + MINUTE)), v2.WalletConfig(max_positions=1))
        self.same(opens(setup()), v2.WalletConfig(max_exposure_multiple=Fraction(1, 10)))
        self.same(opens(setup(stop_distance=10 ** 5)),
                  v2.WalletConfig(initial_balance_e8=10 ** 6, leverage_cap=1, max_exposure_multiple=Fraction(1000)))
        ledger = self.same(opens(setup(status="V")))
        self.assertEqual(ledger[0]["reason"], "not_tradeable")

    def test_liquidation_event_and_the_ignored_stop_close(self):
        config = v2.WalletConfig(mmr=Fraction(1, 50))
        events = [*opens(setup(side=-1)), {"type": "close", "ms": START + MINUTE, "setup_id": "s1", "outcome": "S"},
                  {"type": "liquidation", "ms": START + MINUTE, "setup_id": "s1"}]
        ledger = self.same(events, config)
        self.assertEqual([line["type"] for line in ledger], ["open_fee", "liquidation"])

    def test_portfolio_composition_matches_across_three_configurations(self):
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")

        def signals(symbol, bars, *args):
            return [Signal(symbol, "test:15", "test", symbol, START, 1, 15)], {}

        def setups(signal, *args, **kwargs):
            return [replace(split_setup(signal.symbol, signal.symbol, START), window_minutes=7)]

        inputs = []
        for symbol in symbols:
            data = rows()
            if symbol == "BTCUSDT":
                data[4] = {**data[4], "low": 1.0}
            elif symbol == "ETHUSDT":
                data[4] = {**data[4], "high": 105.0}
            elif symbol == "SOLUSDT":
                data[4] = {**data[4], "low": 97.0}
            inputs.append((symbol, data, [{"calc_time_ms": START + 2 * MINUTE, "rate": "0.001", "mark": "100"}]))
        kwargs = dict(strategy_ids=[], from_ms=START - MINUTE, to_ms=START + 7 * MINUTE)
        with patch.object(engine, "generate_signals", side_effect=signals), patch.object(
                engine, "build_setup", side_effect=setups):
            parts = [engine.market_evaluate(symbol, data, funding, **kwargs) for symbol, data, funding in inputs]
        events = [event for part in parts for event in part["events"]]
        paths = [path for part in parts for path in part["adverse_paths"]]
        seen = set()
        for cap, buffer, mmr in ((5, 1, "0.005"), (10, 2, "0.01"), (20, 3, "0.02")):
            config = v2.WalletConfig(leverage_cap=cap, liquidation_buffer=buffer, mmr=Fraction(mmr),
                                     max_positions=3, max_exposure_multiple=Fraction(20))
            config3 = v3_of(config)
            expected = v2.wallet_step(v2.initial_state(config), reversed(events), config, adverse_paths=paths)
            actual = v3.wallet_step(v3.initial_state(config3), reversed(events), config3, adverse_paths=paths)
            self.assertEqual(actual[1], expected[1])
            for key in V2_STATE_KEYS:
                self.assertEqual(actual[0][key], expected[0][key], key)
            seen |= {line["type"] for line in actual[1]}
        self.assertTrue({"liquidation", "rejected", "pnl", "funding"} <= seen)

class MarkToMarketTests(unittest.TestCase):
    def opened(self, *setups, config=v3.WalletV3Config()):
        return v3.step(v3.initial_state(config), opens(*setups), config)

    def test_long_and_short_unrealized_equity_and_separate_quantities(self):
        state, ledger = self.opened(setup("long", 1), setup("short", -1))
        up = P0 + 2 * 10 ** 8
        state, _ = v3.step(state, [mark(START + MINUTE, up)])
        summary = v3.account_summary(state)
        by_id = {p["setup_id"]: p for p in summary["positions"]}
        for setup_id, side in (("long", 1), ("short", -1)):
            position = state["positions"][setup_id]
            expected = round(side * exact_from_str(position["qty"]) * (up - exact_from_str(position["entry_fill"])))
            self.assertEqual(by_id[setup_id]["unrealized_e8"], expected)
        self.assertGreater(by_id["long"]["unrealized_e8"], 0)   # a rising mark helps the long ...
        self.assertLess(by_id["short"]["unrealized_e8"], 0)     # ... and hurts the short
        self.assertEqual(summary["unrealized_e8"], sum(p["unrealized_e8"] for p in summary["positions"]))
        self.assertEqual(summary["equity_e8"], summary["balance_e8"] + summary["unrealized_e8"])
        margin = sum(p["margin_e8"] for p in state["positions"].values())
        self.assertEqual((summary["used_margin_e8"], summary["available_e8"]),
                         (margin, summary["balance_e8"] - margin))
        self.assertEqual(summary["fees_e8"], sum(line["amount_e8"] for line in ledger))
        self.assertEqual((summary["realized_e8"], summary["funding_e8"], summary["liquidation_e8"]), (0, 0, 0))
        self.assertGreater(summary["maintenance_margin_e8"], 0)
        self.assertGreater(summary["notional_exposure_e8"], 0)
        down, _ = v3.step(state, [mark(START + 2 * MINUTE, P0 - 2 * 10 ** 8)])
        flipped = {p["setup_id"]: p for p in v3.account_summary(down)["positions"]}
        self.assertLess(flipped["long"]["unrealized_e8"], 0)
        self.assertGreater(flipped["short"]["unrealized_e8"], 0)

    def test_marks_are_idempotent_never_go_back_and_staleness_is_explicit(self):
        state, _ = self.opened(setup())
        before = v3.account_summary(state)
        self.assertEqual((before["positions"][0]["mark"], before["positions"][0]["unrealized_e8"]), (None, None))
        self.assertEqual((before["unmarked_positions"], before["equity_e8"]), (1, before["balance_e8"]))
        newer = mark(START + 5 * MINUTE, P0 + 10 ** 8)
        state, _ = v3.step(state, [newer, newer])
        state, _ = v3.step(state, [mark(START + MINUTE, P0 - 10 ** 8)])  # an older mark arriving later is ignored
        self.assertEqual(state["marks"]["BTCUSDT"], {"ms": START + 5 * MINUTE, "price": P0 + 10 ** 8})
        fresh = v3.account_summary(state, as_of_ms=START + 6 * MINUTE, max_mark_age_ms=2 * MINUTE)
        late = v3.account_summary(state, as_of_ms=START + 60 * MINUTE, max_mark_age_ms=2 * MINUTE)
        self.assertEqual((fresh["positions"][0]["mark_stale"], late["positions"][0]["mark_stale"]), (False, True))
        self.assertIsNone(v3.account_summary(state)["positions"][0]["mark_stale"])

class FillTests(unittest.TestCase):
    def open_long(self, config=v3.WalletV3Config(max_exposure_multiple=Fraction(50))):
        state, ledger = v3.step(v3.initial_state(config), opens(setup()), config)
        return state, [ledger], config

    def test_scale_in_then_partial_exit_keeps_quantity_weighted_entry_and_margin_exact(self):
        state, ledgers, config = self.open_long()
        position = state["positions"]["s1"]
        q0, entry0, margin0 = exact_from_str(position["qty"]), exact_from_str(position["entry_fill"]), position["margin_e8"]
        leverage = position["leverage"]
        # Scale in with the same quantity at 110 (reference): the entry is the exact weighted average.
        state, lines = v3.step(state, [fill("in", "increase", position["qty"], 110 * 10 ** 8, START + MINUTE)], config)
        ledgers.append(lines)
        slipped = 110 * 10 ** 8 * (1 + Fraction(1, 10_000))
        position = state["positions"]["s1"]
        self.assertEqual(exact_from_str(position["qty"]), 2 * q0)
        self.assertEqual(exact_from_str(position["entry_fill"]), (q0 * entry0 + q0 * slipped) / (2 * q0))
        self.assertEqual([line["type"] for line in lines], ["open_fee"])
        self.assertEqual(lines[0]["amount_e8"], -round(q0 * slipped * v3.WalletV3Config().taker_rate))
        self.assertEqual(lines[0]["position_event"], "increased")
        self.assertEqual(position["margin_e8"] - margin0, ceil(q0 * slipped / leverage))  # rounded up, as at the open
        self.assertEqual(state["used_margin_e8"], position["margin_e8"])
        # Reduce by a quarter of the open quantity at 120: realized P&L only on the reduced part.
        entry = exact_from_str(position["entry_fill"])
        part = str(2 * q0 / 4)
        exit_fill = 120 * 10 ** 8 * (1 - Fraction(1, 10_000))
        held_margin = position["margin_e8"]
        state, lines = v3.step(state, [fill("out1", "reduce", part, 120 * 10 ** 8, START + 2 * MINUTE, outcome="T")], config)
        ledgers.append(lines)
        self.assertEqual([line["type"] for line in lines], ["pnl", "close_fee"])
        self.assertEqual(lines[0]["amount_e8"], round(exact_from_str(part) * (exit_fill - entry)))
        self.assertEqual(lines[1]["amount_e8"], -round(exact_from_str(part) * exit_fill * v3.WalletV3Config().taker_rate))
        released = (held_margin * exact_from_str(part)) // (2 * q0)  # floor, pro rata
        self.assertEqual(lines[0]["margin_released_e8"], released)
        position = state["positions"]["s1"]
        self.assertEqual(exact_from_str(position["qty"]), 2 * q0 - exact_from_str(part))
        self.assertEqual(exact_from_str(position["entry_fill"]), entry)  # a reduction never moves the average
        self.assertEqual((position["margin_e8"], state["used_margin_e8"]), (held_margin - released,) * 2)
        # Closing the rest releases whatever margin is left, to the unit, and ends the position.
        rest = position["qty"]
        state, lines = v3.step(state, [fill("out2", "reduce", rest, 120 * 10 ** 8, START + 3 * MINUTE)], config)
        ledgers.append(lines)
        self.assertEqual((state["positions"], state["used_margin_e8"], "s1" in state["done"]), ({}, 0, True))
        self.assertEqual(lines[0]["position_event"], "closed")
        flat = [line for ledger in ledgers for line in ledger]
        report = reconcile(config.initial_balance_e8, flat, state)
        self.assertTrue(report["ok"], report)
        totals = state["totals"]
        self.assertEqual(config.initial_balance_e8 + totals["realized_e8"] + totals["fees_e8"], state["balance_e8"])
        self.assertGreater(totals["slippage_e8"], 0)

    def test_over_close_is_rejected_with_a_reason_and_changes_no_money(self):
        state, _, config = self.open_long()
        qty = exact_from_str(state["positions"]["s1"]["qty"])
        after, lines = v3.step(state, [fill("big", "reduce", str(qty + 1), P0, START + MINUTE)], config)
        self.assertEqual([(line["type"], line["reason"], line["amount_e8"]) for line in lines],
                         [("rejected", "over_close", 0)])
        for key in ("balance_e8", "used_margin_e8", "positions"):
            self.assertEqual(after[key], state[key])
        # the rejected id is spent: a retry with a valid quantity under the same id does nothing
        again, retry = v3.step(after, [fill("big", "reduce", str(qty), P0, START + 2 * MINUTE)], config)
        self.assertEqual((retry, again["positions"]), ([], state["positions"]))
        _, lines = v3.step(state, [fill("zero", "reduce", "0", P0, START + MINUTE),
                                   fill("ghost", "reduce", "1", P0, START + MINUTE, setup_id="nope")], config)
        self.assertEqual(sorted(line["reason"] for line in lines), ["invalid_fill_quantity", "no_open_position"])
        with self.assertRaises(ValueError):
            v3.step(state, [fill("odd", "sideways", "1", P0, START + MINUTE)], config)

    def test_duplicate_fill_id_cannot_duplicate_money_or_quantity(self):
        state, _, config = self.open_long()
        event = fill("once", "reduce", str(exact_from_str(state["positions"]["s1"]["qty"]) / 4), P0 + 10 ** 8, START + MINUTE)
        first, lines = v3.step(state, [event, event], config)
        self.assertEqual([line["type"] for line in lines], ["pnl", "close_fee"])
        replay, again = v3.step(first, [event], config)
        self.assertEqual(again, [])
        self.assertEqual({key: replay[key] for key in V2_STATE_KEYS}, {key: first[key] for key in V2_STATE_KEYS})
        self.assertEqual(first["fill_ids"], ["once"])

    def test_available_margin_cannot_be_spent_twice_by_concurrent_setups(self):
        config = v3.WalletV3Config(leverage_cap=1, max_exposure_multiple=Fraction(1000))
        a, b = setup("a", stop_distance=10 ** 8), setup("b", stop_distance=10 ** 8)  # at 1x each needs almost the wallet
        state, ledger = v3.step(v3.initial_state(config), opens(a, b), config)
        self.assertEqual([line["type"] for line in ledger], ["open_fee", "rejected"])
        self.assertEqual(ledger[1]["reason"], "insufficient_margin")
        self.assertLessEqual(state["used_margin_e8"], state["balance_e8"])
        # the same two setups arriving through the portfolio composition (one stage, same millisecond)
        merged = [{**event, "symbol": "BTCUSDT", "sequence": i, "stage_run_id": STAGE}
                  for i, event in enumerate(opens(a, b))]
        _, composed = v3.wallet_step(v3.initial_state(config), merged, config)
        self.assertEqual([line["type"] for line in composed], ["open_fee", "rejected"])
        # and an increase cannot spend what the first position already committed
        _, lines = v3.step(state, [fill("more", "increase", "10", P0, START + MINUTE, setup_id="a")], config)
        self.assertEqual(lines[0]["reason"], "insufficient_margin")

class ReconcileTests(unittest.TestCase):
    def history(self):
        config = v3.WalletV3Config(max_exposure_multiple=Fraction(50))
        state, one = v3.step(v3.initial_state(config), opens(setup("s1"), setup("s2", -1)), config)
        state, two = v3.step(state, [fill("a", "reduce", str(exact_from_str(state["positions"]["s1"]["qty"]) / 3),
                                          P0 + 10 ** 8, START + MINUTE),
                                     {"type": "funding", "ms": START + 60 * MINUTE, "symbol": "BTCUSDT",
                                      "rate": "0.0001", "mark": P0}], config)
        state, three = v3.step(state, [{"type": "close", "ms": START + 90 * MINUTE, "setup_id": "s2", "outcome": "S",
                                        "exit_ref_price": P0 + 3 * 10 ** 8}], config)
        return config, state, [*one, *two, *three]

    def test_a_consistent_history_reconciles_and_the_function_never_mutates(self):
        config, state, ledger = self.history()
        before = deepcopy((state, ledger))
        report = reconcile(config.initial_balance_e8, ledger, state)
        self.assertEqual((report["ok"], report["mismatches"], report["lines"], report["positions"]), (True, [], len(ledger), 1))
        self.assertEqual((state, ledger), before)
        # one step's lines against the state before them
        slice_start = 3
        prior = ledger[slice_start - 1]["balance_e8"]
        self.assertTrue(reconcile(prior, ledger[slice_start:], state, first_seq=slice_start + 1)["ok"])

    def test_a_hand_corrupted_ledger_and_state_are_reported_not_corrected(self):
        config, state, ledger = self.history()
        initial = config.initial_balance_e8
        bad = deepcopy(ledger)
        bad[2]["balance_e8"] += 7                     # a wrong running balance (and every later line stays "right")
        report = reconcile(initial, bad, state)
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"], {"line_balance": 1})  # the state itself still agrees with the amounts
        gap = [line for line in ledger if line["seq"] != 3]
        counts = reconcile(initial, gap, state)["counts"]
        self.assertIn("seq", counts)
        self.assertIn("state_balance", counts)
        twice = [*ledger, deepcopy(next(line for line in ledger if line.get("fill_id")))]
        self.assertIn("duplicate_fill", reconcile(initial, twice, state)["counts"])
        broken = deepcopy(state)
        broken["used_margin_e8"] += 1
        broken["positions"]["s1"]["qty"] = "-1/2"
        broken["balance_e8"] = 0
        counts = reconcile(initial, ledger, broken)["counts"]
        self.assertTrue({"used_margin", "position_quantity", "negative_available", "state_balance", "totals"} <= set(counts))
        reserved = deepcopy(state)
        reserved["reserved_e8"] = 5
        self.assertIn("reserved_margin", reconcile(initial, ledger, reserved)["counts"])

    def test_wallet_v2_ledgers_and_states_reconcile_too(self):
        config = v2.WalletConfig()
        state, first = v2.step(v2.initial_state(config), opens(setup()), config)
        state, second = v2.step(state, [{"type": "close", "ms": START + MINUTE, "setup_id": "s1", "outcome": "T",
                                         "exit_ref_price": P0 + 6 * 10 ** 8}], config)
        self.assertTrue(reconcile(config.initial_balance_e8, [*first, *second], state)["ok"])
        state["used_margin_e8"] = 3
        self.assertEqual(reconcile(config.initial_balance_e8, [*first, *second], state)["counts"], {"used_margin": 1})


class ApiTests(unittest.TestCase):
    def run_scenario(self, scenario):
        """Run ``async scenario(send)`` against one app instance; ``send(body)`` posts to wallet_step."""
        try:
            import asyncio
            import httpx
            from market_analysis.api import create_app
        except ImportError:
            self.skipTest("API dependencies are not installed")
        token = "test-service-token-" + "x" * 32

        async def main():
            app = create_app(token)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    async def send(body):
                        return await client.post("/v1/forward/wallet_step", json=body,
                                                 headers={"Authorization": f"Bearer {token}"})
                    return await scenario(send)

        return asyncio.run(main())

    def event(self, sequence, **payload):
        return {"symbol": "BTCUSDT", "sequence": sequence, "stage_run_id": STAGE, **payload}

    def test_wallet_v2_stays_v2_and_refuses_v3_inputs(self):
        async def scenario(send):
            plain = await send({"schema_version": 1, "events": [self.event(0, type="open", ms=START, setup=setup())]})
            self.assertEqual(plain.status_code, 200, plain.text)
            data = plain.json()
            self.assertEqual(data["wallet_state"]["version"], "wallet-v2")
            self.assertNotIn("account", data)
            mark_event = [self.event(0, type="mark", ms=START, mark=P0)]
            self.assertEqual((await send({"schema_version": 1, "events": mark_event})).status_code, 409)
            self.assertEqual((await send({"schema_version": 1, "events": [],
                                          "config": {"sizing_basis": "equity"}})).status_code, 422)
            self.assertEqual((await send({"schema_version": 1, "events": [], "state": v3.initial_state()})).status_code, 409)
            self.assertEqual((await send({"schema_version": 1, "events": [], "state": data["wallet_state"],
                                          "config": {"wallet_version": "wallet-v3"}})).status_code, 409)
            check = await send({"schema_version": 1, "events": [], "state": data["wallet_state"],
                                "reconcile": {"initial_balance_e8": 100 * 10 ** 8, "ledger": data["ledger"]}})
            self.assertTrue(check.json()["full_reconciliation"]["ok"])

        self.run_scenario(scenario)


if __name__ == "__main__":
    unittest.main()
