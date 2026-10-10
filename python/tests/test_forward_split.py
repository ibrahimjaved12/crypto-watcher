"""Small synthetic split regressions; no long sigma warm-up or external processes."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import importlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from market_analysis.forward import wallet
from market_analysis.forward.setups import Setup, setup_id
from market_analysis.forward.signals import Signal

engine = importlib.import_module("market_analysis.forward.evaluate")
FIXTURES = Path(__file__).with_name("fixtures")
START = 1735689600000
MINUTE = 60_000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")


def original_engine():
    # Frozen at the start of P27; relative imports intentionally target the production package.
    spec = importlib.util.spec_from_file_location("market_analysis.forward._before_p27",
                                                  FIXTURES / "forward_evaluate_before_p27.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows(count=8):
    return [{"open_time_ms": START + i * MINUTE, "open": 100.0, "high": 100.1,
             "low": 99.9, "close": 100.0, "transport": "rest"} for i in range(count)]


def setup(symbol="BTCUSDT", name="pending", entry=START + 5 * MINUTE):
    return Setup(setup_id(name, Fraction(2), Fraction(2), ""), name, "test:15", "test", symbol, 1, 15, entry, entry, "2", "2", "T", 1,
                 60, p0=100 * 10**8, stop=98 * 10**8, target=104 * 10**8, tick=10**6, d_ticks=200)


def held_position():
    return {"symbol": "BTCUSDT", "side": 1, "entry_ms": START, "stop": 98 * 10**8,
            "target": 104 * 10**8, "qty": "1", "entry_fill": str(100 * 10**8),
            "leverage": 10, "leverage_cap": 20, "liquidation_rule": wallet.WALLET_VERSION,
            "liquidation_price": str(90 * 10**8), "margin_e8": 10**9, "notional_e8": str(100 * 10**8)}


class SplitTests(unittest.TestCase):
    def test_hand_derived_golden_and_complete_original_response(self):
        fixture = json.loads((FIXTURES / "forward_split_golden.json").read_text())
        state = wallet.initial_state()
        state["positions"]["held"] = held_position()
        state["used_margin_e8"] = 10**9
        call = dict(symbols=[{"symbol": fixture["symbol"], "rows": rows(fixture["row_count"]),
                             "funding": [{"calc_time_ms": START + MINUTE, "rate": "0.001", "mark": "110"}]}],
                    strategy_ids=[], from_ms=START, to_ms=START + 2 * MINUTE,
                    open_setups=[{"setup": setup().to_dict()}], wallet_state=state)
        before = deepcopy(call)
        expected = original_engine().evaluate(**call)
        actual = engine.evaluate(**call)
        self.assertEqual(actual, expected)
        self.assertEqual({key: actual[key] for key in fixture["expected"]}, fixture["expected"])
        self.assertEqual(actual["wallet_state"], {**state, "seq": 1, "balance_e8": 9989000000})
        self.assertEqual(call, before)

    def test_six_symbols_equal_individual_market_slices(self):
        saved = [{"setup": setup(symbol, symbol).to_dict()} for symbol in SYMBOLS]
        inputs = [{"symbol": symbol, "rows": rows(), "funding_available": symbol != "ETHUSDT"}
                  for symbol in SYMBOLS]
        kwargs = dict(strategy_ids=[], from_ms=START, to_ms=START + 7 * MINUTE, open_setups=saved)
        full = engine.evaluate(inputs, **kwargs)
        self.assertEqual(full, original_engine().evaluate(inputs, **kwargs))
        all_events = []
        for item in inputs:
            part = engine.market_evaluate(item["symbol"], item["rows"],
                                          funding_available=item["funding_available"], **kwargs)
            for key in ("signals", "setups"):
                self.assertEqual(part[key], [value for value in full[key] if value["symbol"] == item["symbol"]])
            self.assertEqual(part["resolutions"], [value for value in full["resolutions"]
                                                  if value["setup_id"] == setup(item["symbol"], item["symbol"]).setup_id])
            self.assertEqual(part["reasons"], full["reasons"][item["symbol"]])
            self.assertEqual(part["processed_to_ms"], full["processed_to_ms"][item["symbol"]])
            self.assertEqual(part["events"], sorted(part["events"], key=lambda e: (e["ms"], e["symbol"], e["sequence"])))
            all_events.extend(part["events"])
        state, ledger = wallet.wallet_step(wallet.initial_state(), reversed(all_events))
        self.assertEqual((state, ledger), (full["wallet_state"], full["ledger"]))

    def test_generated_entries_exits_funding_liquidation_and_caps_match_original(self):
        # Replace only signal discovery/geometry to avoid 90+ days of sigma warm-up.
        # Adapters, resolution, funding, liquidation and wallet accounting remain real.
        old = original_engine()
        def signals(symbol, bars, *args):
            return [Signal(symbol, "test:15", "test", symbol, START, 1, 15)], {}
        def setups(signal, *args, **kwargs):
            return [replace(setup(signal.symbol, signal.symbol + str(i), START), window_minutes=7)
                    for i in range(2)]
        for cap in (1, 6):
            for reverse in (False, True):
                with self.subTest(cap=cap, reverse=reverse), ExitStack() as patches:
                    for module in (old, engine):
                        patches.enter_context(patch.object(module, "generate_signals", side_effect=signals))
                        patches.enter_context(patch.object(module, "build_setup", side_effect=setups))
                    inputs = []
                    for symbol in SYMBOLS:
                        data = rows()
                        if symbol == "BTCUSDT":
                            data[4] = {**data[4], "low": 80.0}  # liquidation at the stop minute
                        inputs.append({"symbol": symbol, "rows": data,
                                       "funding": [{"calc_time_ms": START + 2 * MINUTE,
                                                    "rate": "0.001", "mark": "110"}]})
                    if reverse:
                        inputs.reverse()
                    kwargs = dict(strategy_ids=[], from_ms=START - MINUTE, to_ms=START + 7 * MINUTE,
                                  wallet_config=wallet.WalletConfig(max_positions=cap))
                    actual = engine.evaluate(inputs, **kwargs)
                    self.assertEqual(actual, old.evaluate(inputs, **kwargs))
                    for item in inputs:
                        part = engine.market_evaluate(item["symbol"], item["rows"], item["funding"], **kwargs)
                        for key in ("signals", "setups"):
                            self.assertEqual(part[key], [v for v in actual[key] if v["symbol"] == item["symbol"]])
                        ids = {v["setup_id"] for v in part["setups"]}
                        self.assertEqual(part["resolutions"], [v for v in actual["resolutions"] if v["setup_id"] in ids])

    def test_wallet_merge_is_pure_deterministic_and_keeps_settlement_priority(self):
        state = wallet.initial_state()
        state["positions"]["held"] = held_position()
        state["used_margin_e8"] = 10**9
        events = [{"type": "funding", "ms": START + MINUTE, "symbol": "BTCUSDT", "sequence": 0,
                   "rate": "0.001", "mark": 100 * 10**8},
                  {"type": "close", "ms": START + MINUTE, "symbol": "BTCUSDT", "sequence": 1,
                   "setup_id": "held", "exit_ref_price": 100 * 10**8, "outcome": "E"}]
        before = deepcopy((state, events))
        actual = wallet.wallet_step(state, events)
        self.assertEqual(actual, wallet.step(state, events))
        self.assertEqual(actual, wallet.wallet_step(state, reversed(events)))
        self.assertEqual([line["type"] for line in actual[1]], ["pnl", "close_fee"])
        self.assertEqual((state, events), before)


class SplitApiTests(unittest.TestCase):
    def test_sync_split_routes_auth_validation_and_composition(self):
        try:
            import asyncio
            import httpx
            from market_analysis.api import create_app
            from market_analysis.api_models import ForwardWalletConfig
        except ImportError:
            self.skipTest("API dependencies are not installed")
        self.assertEqual(ForwardWalletConfig().config(), wallet.WalletConfig())
        token = "test-service-token-" + "x" * 32
        body = {"schema_version": 1, "symbol": "BTCUSDT", "rows": rows(),
                "strategy_ids": ["ema_cross_20_50:15"], "from_ms": START, "to_ms": START + 7 * MINUTE}

        async def exercise():
            app = create_app(token)
            for route in app.routes:
                if getattr(route, "path", "") in ("/v1/forward/market_evaluate", "/v1/forward/wallet_step"):
                    self.assertFalse(asyncio.iscoroutinefunction(route.endpoint))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    market_path, wallet_path = "/v1/forward/market_evaluate", "/v1/forward/wallet_step"
                    self.assertEqual((await client.post(market_path, json=body)).status_code, 401)
                    self.assertEqual((await client.post(wallet_path, json={"schema_version": 1, "events": []})).status_code, 401)
                    client.headers["Authorization"] = f"Bearer {token}"
                    response = await client.post(market_path, json=body)
                    self.assertEqual(response.status_code, 200, response.text)
                    market = response.json()
                    response = await client.post(wallet_path, json={"schema_version": 1, "events": market["events"]})
                    self.assertEqual(response.status_code, 200, response.text)
                    combined = await client.post("/v1/forward/evaluate", json={
                        "schema_version": 1, "symbols": [{"symbol": body["symbol"], "rows": body["rows"]}],
                        "strategy_ids": body["strategy_ids"], "from_ms": body["from_ms"], "to_ms": body["to_ms"]})
                    self.assertEqual(combined.status_code, 200, combined.text)
                    for key in ("wallet_state", "ledger"):
                        self.assertEqual(response.json()[key], combined.json()[key])
                    self.assertEqual(market["resolutions"], combined.json()["resolutions"])
                    bad = await client.post(wallet_path, json={"schema_version": 1, "events": [
                        {"type": "funding", "ms": START, "symbol": "BTCUSDT", "sequence": 0}]})
                    self.assertEqual(bad.status_code, 422)
                    bad = await client.post(market_path, json={**body, "to_ms": START - 1})
                    self.assertEqual(bad.status_code, 422)
        asyncio.run(exercise())
