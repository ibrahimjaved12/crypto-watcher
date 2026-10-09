"""Paper wallet step (#239 P10): leverage rule, caps, fees, funding, liquidation before stop, reconciliation."""
from __future__ import annotations

from array import array
from fractions import Fraction
import unittest

from market_analysis import data_lake
from market_analysis.benchmark import costs
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.canonical import exact_from_str
from market_analysis.forward import wallet as wl

START = data_lake.month_bounds_ms("2025-01")[0]
MINUTE = 60_000
P0 = 100 * 10 ** 8  # 100 USDT


def setup(setup_id="s1", side=1, stop_distance=2 * 10 ** 8, symbol="BTCUSDT", entry_ms=START, status="T"):
    return {"setup_id": setup_id, "symbol": symbol, "side": side, "entry_ms": entry_ms, "p0": P0,
            "stop": P0 - side * stop_distance, "target": P0 + side * 3 * stop_distance, "status": status}


def opened(state, *setups, config=wl.WalletConfig()):
    return wl.step(state, [{"type": "open", "ms": s["entry_ms"], "setup": s} for s in setups], config)


def reconcile(case, state, ledgers, config=wl.WalletConfig()):
    total = sum(line["amount_e8"] for ledger in ledgers for line in ledger)
    case.assertEqual(config.initial_balance_e8 + total, state["balance_e8"])
    seqs = [line["seq"] for ledger in ledgers for line in ledger]
    case.assertEqual(seqs, list(range(1, len(seqs) + 1)))


class WalletTests(unittest.TestCase):
    def test_leverage_rule_sizing_and_fees(self):
        state, ledger = opened(wl.initial_state(), setup())
        position = state["positions"]["s1"]
        fill = exact_from_str(position["entry_fill"])
        stop_distance = fill - (P0 - 2 * 10 ** 8)
        leverage = position["leverage"]
        lp = exact_from_str(position["liquidation_price"])
        self.assertGreaterEqual(fill - lp, 2 * stop_distance)  # liquidation at least 2x the stop distance away
        if leverage < wl.WalletConfig().leverage_cap:
            worse = costs.liquidation_price(1, fill, leverage + 1, wl.WalletConfig().mmr)
            self.assertLess(fill - worse, 2 * stop_distance)  # and it is the largest such leverage
        qty = exact_from_str(position["qty"])
        self.assertEqual(qty * stop_distance, Fraction(wl.WalletConfig().initial_balance_e8, 100))  # 1 % = 1R
        self.assertEqual(ledger[0]["type"], "open_fee")
        self.assertEqual(ledger[0]["amount_e8"], -round(qty * fill * wl.WalletConfig().taker_rate))
        self.assertIn("flat-tier", ledger[0]["assumptions"])
        self.assertGreater(fill, P0)  # slippage floor against a long entry

    def test_close_pnl_fees_and_reconciliation(self):
        state, first = opened(wl.initial_state(), setup())
        state, second = wl.step(state, [{"type": "close", "ms": START + 9 * MINUTE, "setup_id": "s1",
                                         "outcome": "T", "exit_ref_price": P0 + 6 * 10 ** 8}])
        self.assertEqual([line["type"] for line in second], ["pnl", "close_fee"])
        self.assertGreater(second[0]["amount_e8"], 0)
        self.assertLess(second[1]["amount_e8"], 0)
        self.assertEqual((state["positions"], state["used_margin_e8"]), ({}, 0))
        reconcile(self, state, [first, second])
        again, repeat = wl.step(state, [{"type": "close", "ms": START + 9 * MINUTE, "setup_id": "s1",
                                         "outcome": "T", "exit_ref_price": P0}])
        self.assertEqual((repeat, again["balance_e8"]), ([], state["balance_e8"]))  # idempotent
        _, reopened = opened(again, setup())
        self.assertEqual(reopened, [])

    def test_funding_sign_for_long_and_short(self):
        state, first = opened(wl.initial_state(), setup("long", 1), setup("short", -1, symbol="BTCUSDT"))
        state, ledger = wl.step(state, [{"type": "funding", "ms": START + 60 * MINUTE, "symbol": "BTCUSDT",
                                         "rate": "0.0001", "mark": P0}])
        by_setup = {line["setup_id"]: line["amount_e8"] for line in ledger}
        self.assertLess(by_setup["long"], 0)     # longs pay a positive rate
        self.assertGreater(by_setup["short"], 0)  # shorts receive it
        reconcile(self, state, [first, ledger])

    def test_caps_and_insufficient_margin_are_recorded_rejections(self):
        config = wl.WalletConfig(max_positions=1)
        state, ledger = opened(wl.initial_state(config), setup("a"), setup("b", entry_ms=START + MINUTE), config=config)
        self.assertEqual([line["type"] for line in ledger], ["open_fee", "rejected"])
        self.assertEqual(ledger[1]["reason"], "cap:max_positions")
        tight = wl.WalletConfig(max_exposure_multiple=Fraction(1, 10))
        _, rejected = opened(wl.initial_state(tight), setup(), config=tight)
        self.assertEqual(rejected[0]["reason"], "cap:total_exposure")
        poor = wl.WalletConfig(initial_balance_e8=10 ** 6, leverage_cap=1, max_exposure_multiple=Fraction(1000))
        _, rejected = opened(wl.initial_state(poor), setup(stop_distance=10 ** 5), config=poor)
        self.assertEqual(rejected[0]["reason"], "insufficient_margin")
        _, rejected = opened(wl.initial_state(), setup(status="V"))
        self.assertEqual((rejected[0]["reason"], rejected[0]["amount_e8"]), ("not_tradeable", 0))

    def test_liquidation_before_the_stop(self):
        state, first = opened(wl.initial_state(), setup())
        position = state["positions"]["s1"]
        lp = exact_from_str(position["liquidation_price"])
        minutes = 5
        prices = [P0] * minutes
        lows = list(prices)
        lows[3] = int(lp) - 10 ** 6  # a gap straight through the liquidation price (and the stop)
        columns = [array("q", prices), array("q", prices), array("q", lows), array("q", prices)]
        bars = BarSeries("BTCUSDT", START, minutes, *columns, *[array("q", [0] * minutes) for _ in range(3)],
                         *[array("q", values) for values in (prices, prices, lows, prices)], array("H", [0] * minutes))
        events = wl.liquidation_events(state["positions"], bars, {"s1": {"exit_ms": START + 3 * MINUTE}},
                                       wl.WalletConfig())
        self.assertEqual(events, [{"type": "liquidation", "ms": START + 3 * MINUTE, "setup_id": "s1"}])
        close = {"type": "close", "ms": START + 3 * MINUTE, "setup_id": "s1", "outcome": "S",
                 "exit_ref_price": lows[3]}
        state, ledger = wl.step(state, [close, *events])
        self.assertEqual([line["type"] for line in ledger], ["liquidation"])  # the stop's close is ignored
        self.assertEqual(ledger[0]["amount_e8"], -position["margin_e8"])  # isolated: the margin is lost
        self.assertEqual(state["used_margin_e8"], 0)
        reconcile(self, state, [first, ledger])


class FundingAvailabilityTests(unittest.TestCase):
    def test_windows_containing_a_possible_funding_time(self):
        from market_analysis.forward.evaluate import crosses_funding_time
        hour = 3_600_000
        self.assertFalse(crosses_funding_time(START + 5 * MINUTE, START + 50 * MINUTE))
        self.assertTrue(crosses_funding_time(START + 50 * MINUTE, START + hour))        # exit at the settlement
        self.assertTrue(crosses_funding_time(START + 50 * MINUTE, START + hour + MINUTE))
        self.assertFalse(crosses_funding_time(START + hour, START + hour + 30 * MINUTE))  # entry at it: not inside


class ApiTests(unittest.TestCase):
    def test_forward_evaluate_endpoint(self):
        try:
            import asyncio
            import httpx
            from market_analysis.api import create_app
        except ImportError:
            self.skipTest("API dependencies are not installed")
        token = "test-service-token-" + "x" * 32
        rows = [{"open_time_ms": START + i * MINUTE, "open": 100.0 + (i % 7) * 0.1, "high": 101.0,
                 "low": 99.0, "close": 100.0 + (i % 5) * 0.1, "volume": 1.0, "transport": "rest"}
                for i in range(2 * 1440)]
        body = {"schema_version": 1, "symbols": [{"symbol": "BTCUSDT", "rows": rows}],
                "strategy_ids": ["ema_cross_20_50:15", "placebo-v1:ema_cross_20_50:15"],
                "from_ms": START + 1440 * MINUTE, "to_ms": START + 2 * 1440 * MINUTE}

        async def send(payload, headers):
            app = create_app(token)
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    return await client.post("/v1/forward/evaluate", json=payload, headers=headers)

        response = asyncio.run(send(body, {"Authorization": f"Bearer {token}"}))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["versions"]["forward"], "forward-v2")
        self.assertEqual(data["wallet_state"]["balance_e8"], 100 * 10 ** 8)
        self.assertEqual(data["setups"], [])  # P16: no ewma-robust-hcal sigma yet, so no setups at all
        self.assertTrue(all("no_sigma" in r.get("sigma", "no_sigma") for r in data["reasons"].values()))
        self.assertEqual(asyncio.run(send(body, {})).status_code, 401)
        bad = {**body, "symbols": [{"symbol": "BTCUSDT", "rows": [{**rows[0], "open_time_ms": START + 1}]}]}
        self.assertEqual(asyncio.run(send(bad, {"Authorization": f"Bearer {token}"})).status_code, 422)


if __name__ == "__main__":
    unittest.main()
