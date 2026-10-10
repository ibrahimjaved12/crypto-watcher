"""Small synthetic split regressions; no long sigma warm-up or external processes."""
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction
import importlib
import unittest
from unittest.mock import patch

from market_analysis.forward import wallet
from market_analysis.forward.setups import Setup, setup_id
from market_analysis.forward.signals import Signal

engine = importlib.import_module("market_analysis.forward.evaluate")
START = 1735689600000
MINUTE = 60_000
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")


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
    def test_three_wallet_configs_share_market_and_match_legacy_accounting(self):
        # Fix discovery/geometry only. The oracle uses the former opens-only preflight
        # and full mark-series scan, independently of the adverse-path implementation.
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
        def signals(symbol, bars, *args):
            return [Signal(symbol, "test:15", "test", symbol, START, 1, 15)], {}
        def setups(signal, *args, **kwargs):
            return [replace(setup(signal.symbol, signal.symbol, START), window_minutes=7)]
        inputs = []
        for symbol in symbols:
            data = rows()
            if symbol == "BTCUSDT":
                data[4] = {**data[4], "low": 1.0}
            elif symbol == "ETHUSDT":
                data[4] = {**data[4], "high": 105.0}
            elif symbol == "SOLUSDT":
                data[4] = {**data[4], "low": 97.0}
            inputs.append({"symbol": symbol, "rows": data,
                           "funding": [{"calc_time_ms": START + 2 * MINUTE,
                                        "rate": "0.001", "mark": "100"}]})
        kwargs = dict(strategy_ids=[], from_ms=START - MINUTE, to_ms=START + 7 * MINUTE)
        with patch.object(engine, "generate_signals", side_effect=signals), patch.object(
                engine, "build_setup", side_effect=setups):
            parts = [engine.market_evaluate(i["symbol"], i["rows"], i["funding"], **kwargs) for i in inputs]
            events = [e for p in parts for e in p["events"]]
            paths = [p for part in parts for p in part["adverse_paths"]]
            before = deepcopy((events, paths))
            balances = []
            for cap, buffer, mmr in ((5, 1, "0.005"), (10, 2, "0.01"), (20, 3, "0.02")):
                config = wallet.WalletConfig(leverage_cap=cap, liquidation_buffer=buffer,
                    mmr=Fraction(mmr), max_positions=3, max_exposure_multiple=Fraction(20))
                state = wallet.initial_state(config)
                legacy = [{k: v for k, v in e.items() if k not in
                           ("symbol", "sequence", "stage_run_id") or e["type"] == "funding"}
                          for part in parts for e in sorted(part["events"], key=lambda e: e["sequence"])]
                opened, _ = wallet.step(state, [e for e in legacy if e["type"] == "open"], config)
                for item, part in zip(inputs, parts):
                    bars = engine.bars_from_collector_rows(item["symbol"], item["rows"])
                    engine.apply_funding_marks(bars, item["funding"])
                    legacy += wallet.liquidation_events(opened["positions"], bars,
                        {r["setup_id"]: r for r in part["resolutions"]}, config)
                expected = wallet.step(state, legacy, config)
                actual = wallet.wallet_step(state, reversed(events), config, adverse_paths=paths)
                self.assertEqual(actual, expected)
                composed = engine.evaluate(inputs, wallet_config=config, **kwargs)
                self.assertEqual(actual, (composed["wallet_state"], composed["ledger"]))
                self.assertIn("liquidation", [line["type"] for line in actual[1]])
                self.assertIn("rejected", [line["type"] for line in actual[1]])
                self.assertEqual({line["outcome"] for line in actual[1] if line["type"] == "pnl"}, {"S", "T"})
                balances.append(actual[0]["balance_e8"])
            self.assertGreater(len(set(balances)), 1)
            self.assertEqual((events, paths), before)

    def test_never_opened_close_and_path_are_silent_and_duplicate_keys_fail(self):
        state = wallet.initial_state()
        event = {"type": "close", "ms": START, "symbol": "BTCUSDT", "sequence": 0,
                 "stage_run_id": "00000000-0000-0000-0000-000000000001", "setup_id": "absent"}
        path = {"setup_id": "absent", "symbol": "BTCUSDT", "side": 1, "points": [[START, 1]]}
        self.assertEqual(wallet.wallet_step(state, [event], adverse_paths=[path]), (state, []))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            wallet.wallet_step(state, [event, event])
        self.assertEqual(wallet.wallet_step(state, [event, {**event,
            "stage_run_id": "00000000-0000-0000-0000-000000000002"}]), (state, []))

    def test_shared_paths_stop_at_stage_end_and_evaluate_keeps_legacy_window(self):
        data = rows()
        data[6] = {**data[6], "low": 1.0}
        saved = [{"setup": setup(entry=START).to_dict()}]
        args = dict(strategy_ids=[], from_ms=START, to_ms=START + 3 * MINUTE, open_setups=saved)
        shared = engine.market_evaluate("BTCUSDT", data, **args)
        self.assertEqual(shared["resolutions"][0]["status"], "open")
        self.assertTrue(all(ms <= args["to_ms"] for path in shared["adverse_paths"] for ms, _ in path["points"]))
        legacy = engine.evaluate([{"symbol": "BTCUSDT", "rows": data}], **args)
        self.assertEqual(legacy["resolutions"][0]["exit_ms"], START + 6 * MINUTE)

    def test_compatibility_order_only_changes_internal_composition(self):
        config = wallet.WalletConfig(max_positions=1)
        state = wallet.initial_state(config)
        events = [{"type": "open", "ms": START, "symbol": symbol, "sequence": 0,
                   "stage_run_id": "00000000-0000-0000-0000-000000000001",
                   "setup": setup(symbol, symbol, START).to_dict(), "compatibility_order": i}
                  for i, symbol in enumerate(("ETHUSDT", "BTCUSDT"))]
        internal = wallet.wallet_step(state, events, config, compatibility=True)
        legacy = wallet.step(state, [{"type": "open", "ms": START, "setup": e["setup"]} for e in events], config)
        self.assertEqual(internal, legacy)
        transport = wallet.wallet_step(state, events, config)
        self.assertEqual(transport[1][0]["symbol"], "BTCUSDT")
        self.assertEqual(internal[1][0]["symbol"], "ETHUSDT")

    def test_liquidation_eligibility_uses_original_opens_only_preflight(self):
        config = wallet.WalletConfig(max_positions=1)
        state = wallet.initial_state(config)
        state["positions"]["held"] = held_position()
        state["used_margin_e8"] = 10**9
        new = setup(entry=START + MINUTE).to_dict()
        events = [{"type": "close", "ms": START + MINUTE, "setup_id": "held", "exit_ref_price": 100 * 10**8},
                  {"type": "open", "ms": START + MINUTE, "setup": new}]
        events = [{**e, "symbol": "BTCUSDT", "sequence": i,
                   "stage_run_id": "00000000-0000-0000-0000-000000000001"} for i, e in enumerate(events)]
        path = {"setup_id": new["setup_id"], "symbol": "BTCUSDT", "side": 1,
                "points": [[START + 2 * MINUTE, 1]]}
        result, ledger = wallet.wallet_step(state, events, config, adverse_paths=[path])
        # The chronological close frees capacity, but the historical opens-only preflight
        # rejected this entry, so its liquidation candidate must still be filtered out.
        self.assertIn(new["setup_id"], result["positions"])
        self.assertNotIn("liquidation", [line["type"] for line in ledger])

    def test_record_extremes_match_inclusive_long_and_short_mark_thresholds(self):
        bars = engine.bars_from_collector_rows("BTCUSDT", rows())
        for i, (low, high) in enumerate(zip([100, 99, 99, 101, 97, 96, 98, 95],
                                          [100, 101, 101, 99, 103, 104, 102, 105])):
            bars.mark_low[i], bars.mark_high[i] = low, high
        for side, lp in ((1, "195/2"), (-1, "205/2")):
            position = {**held_position(), "side": side, "liquidation_price": lp}
            resolution = {"exit_ms": START + 4 * MINUTE}
            path = wallet.adverse_path({**position, "setup_id": "held"}, bars, resolution)
            self.assertEqual([point[0] for point in path["points"]],
                             [START, START + MINUTE, START + 4 * MINUTE])
            self.assertEqual(wallet.path_liquidations({"held": position}, [path]),
                wallet.liquidation_events({"held": position}, bars, {"held": resolution}, wallet.WalletConfig()))

    def test_six_symbols_equal_individual_market_slices(self):
        saved = [{"setup": setup(symbol, symbol).to_dict()} for symbol in SYMBOLS]
        inputs = [{"symbol": symbol, "rows": rows(), "funding_available": symbol != "ETHUSDT"}
                  for symbol in SYMBOLS]
        kwargs = dict(strategy_ids=[], from_ms=START, to_ms=START + 7 * MINUTE, open_setups=saved)
        full = engine.evaluate(inputs, **kwargs)
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

    def test_wallet_merge_is_pure_deterministic_and_keeps_settlement_priority(self):
        state = wallet.initial_state()
        state["positions"]["held"] = held_position()
        state["used_margin_e8"] = 10**9
        events = [{"type": "funding", "ms": START + MINUTE, "symbol": "BTCUSDT", "sequence": 0,
                   "rate": "0.001", "mark": 100 * 10**8},
                  {"type": "close", "ms": START + MINUTE, "symbol": "BTCUSDT", "sequence": 1,
                   "setup_id": "held", "exit_ref_price": 100 * 10**8, "outcome": "E"}]
        events = [{**e, "stage_run_id": "00000000-0000-0000-0000-000000000001"} for e in events]
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
                "stage_run_id": "00000000-0000-0000-0000-000000000001",
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
                    response = await client.post(wallet_path, json={"schema_version": 1, "events": market["events"], "adverse_paths": market["adverse_paths"]})
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
