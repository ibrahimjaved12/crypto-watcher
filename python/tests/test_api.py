import asyncio
from copy import deepcopy
from decimal import Decimal
import unittest

import httpx

from market_analysis.api import create_app
from market_analysis.core import Candle, MINUTE
from market_analysis.service import analyze_request, load_series, load_workload
from market_analysis.providers import ANALYSIS_PROVIDERS, SOURCE, source_instrument
from market_analysis.technical import TechnicalCandle

NOW = 1704153600000
TOKEN = "test-service-token-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def payload():
    return {"schema_version": 1, "symbol": "BTCUSDT",
            "instrument_id": "binance-usdm:BTCUSDT",
            "settings": {"threshold_pct": "2", "cooldown_minutes": 15, "monitoring_enabled": True},
            "baseline": {"price": "100", "at_ms": NOW - 60 * MINUTE,
                         "source": SOURCE, "threshold": "2",
                         "last_observed_ms": NOW - 5 * MINUTE,
                         "last_up_alert_ms": None, "last_down_alert_ms": None}}


def technical_payload():
    duration = 15 * MINUTE
    candles = [
        {"open_ms": NOW - (220 - index) * duration,
         "open": str(index + 100), "high": str(index + 102),
         "low": str(index + 99), "close": str(index + 101),
         "volume": "100", "complete": True}
        for index in range(220)
    ]
    return {
        "schema_version": 2,
        "instrument": {"instrument_id": "binance-usdm:BTCUSDT",
                       "exchange": "binance-usdm", "native_symbol": "BTCUSDT",
                       "market_type": "futures", "contract_type": "perpetual"},
        "timeframe_minutes": 15, "candles": candles, "warmup_candles": [],
        "missing_open_times_ms": [], "source": "binance-usdm",
        "source_event_time_ms": NOW, "evaluation_time_ms": NOW,
        "detection_time_ms": NOW, "price_type": "trade",
        "config": {"ta_version": "ta-v2", "interpretation_version": "interpretation-v1",
                   "minimum_history": 200},
    }


def movement_payload(session_id="2af3e7c8-b777-4e58-9ad2-18e36daac160"):
    return {
        "schema_version": 1,
        "session_id": session_id,
        "boundary_time_ms": NOW,
        "symbols": [{
            "symbol": "BTCUSDT",
            "instrument_id": "binance-usdm:BTCUSDT",
            "membership_epoch": 1,
            "source_state": "LIVE",
            "observations": [{
                "price": "101",
                "quantity": "2",
                "event_time_ms": NOW + 20,
                "trade_time_ms": NOW,
                "aggregate_trade_id": 17,
                "received_at_ms": NOW + 30,
            }],
        }],
    }


def series(price="102"):
    return {interval: [Candle(NOW - n * interval * MINUTE, Decimal(price))
                       for n in range(count, -1, -1)]
            for interval, count in ((1, 61), (15, 97))}


def workload(provider=SOURCE, symbol="BTCUSDT", price="102"):
    value = float(price)
    technical = {}
    for timeframe in (15, 60, 240):
        duration = timeframe * MINUTE
        technical[timeframe] = tuple(
            TechnicalCandle(
                NOW - (200 - index) * duration,
                value,
                value + 1,
                value - 1,
                value,
                100,
                True,
            )
            for index in range(200)
        )
    return {"rolling": series(price), "technical": technical,
            "source_instrument": source_instrument(provider, symbol)}


async def fixture_loader(client, provider, symbol):
    return workload(provider, symbol)


def analyzer_for(loader=fixture_loader):
    async def analyzer(request, client):
        return await analyze_request(request, client, loader=loader, clock=lambda: NOW)
    return analyzer


class ApiTests(unittest.TestCase):
    def request(self, app, method, path, **kwargs):
        async def send():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport,
                                              base_url="http://testserver") as client:
                    return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def post(self, body=None, loader=fixture_loader):
        return self.request(create_app(TOKEN, analyzer_for(loader)), "POST", "/v1/analysis",
                            json=body or payload(), headers=HEADERS)

    def test_health_and_authentication(self):
        app = create_app(TOKEN, analyzer_for())
        self.assertEqual(self.request(app, "GET", "/health").status_code, 200)
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}):
            response = self.request(app, "POST", "/v1/analysis", json=payload(), headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(
            self.request(app, "POST", "/v1/analysis", json=payload(), headers=HEADERS).status_code,
            200)

    def test_fastapi_uses_shared_technical_calculator(self):
        app = create_app(TOKEN, analyzer_for())
        response = self.request(app, "POST", "/v1/technical-analysis",
                                json=technical_payload(), headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["score"], 60)

    def test_scheduled_batch_targets_due_candles(self):
        app = create_app(TOKEN, analyzer_for())
        latest = technical_payload()
        older = deepcopy(latest)
        target = older["candles"][-8]["open_ms"]
        older["target_candle_open_time_ms"] = target
        older["source_event_time_ms"] = target + 15 * MINUTE
        response = self.request(
            app,
            "POST",
            "/v1/technical-analysis/batch",
            json={"schema_version": 2, "requests": [latest, older]},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["candle_open_time_ms"], latest["candles"][-1]["open_ms"])
        self.assertEqual(results[1]["candle_open_time_ms"], target)

    def test_movement_boundary_uses_python_engine_and_replays_identically(self):
        app = create_app(TOKEN, analyzer_for())
        body = movement_payload()
        first = self.request(
            app,
            "POST",
            "/v1/movement/boundary",
            json=body,
            headers=HEADERS,
        )
        second = self.request(
            app,
            "POST",
            "/v1/movement/boundary",
            json=body,
            headers=HEADERS,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        result = first.json()
        self.assertEqual(result["snapshots"][0]["buckets"][0]["endpointPrice"], 101)
        self.assertEqual(result["snapshots"][0]["buckets"][0]["lastRealTradeTime"], NOW)
        self.assertEqual(result["snapshots"][0]["buckets"][0]["lastRealEventTime"], NOW + 20)
        self.assertEqual(result["snapshots"][0]["buckets"][0]["lastRealReceivedAt"], NOW + 30)
        readiness = result["snapshots"][0]["readiness"]["1"]
        self.assertEqual(readiness["state"], "warming")
        self.assertEqual(readiness["reason"], "insufficient_exact_live_history")
        self.assertEqual(readiness["status"], "WARMING")

    def test_batch_provenance_and_completion_boundary(self):
        app = create_app(TOKEN, analyzer_for())
        duration = 15 * MINUTE
        absent = technical_payload()
        target = absent["candles"][-8]["open_ms"]
        # REST bootstrap/recovery carries no exchange event; a boundary-complete target
        # is still valid.
        absent["target_candle_open_time_ms"] = target
        absent["source_event_time_ms"] = None
        absent["evaluation_time_ms"] = target + duration
        absent["detection_time_ms"] = target + duration
        # A WebSocket event time that differs from the completion boundary is preserved
        # and must not fail.
        shifted = deepcopy(absent)
        shifted["source_event_time_ms"] = target + duration - 7
        # An incomplete target candle is rejected via the deterministic boundary.
        incomplete = deepcopy(absent)
        incomplete["evaluation_time_ms"] = target + duration - 1
        incomplete["detection_time_ms"] = target + duration - 1
        response = self.request(
            app,
            "POST",
            "/v1/technical-analysis/batch",
            json={"schema_version": 2, "requests": [absent, shifted, incomplete]},
            headers=HEADERS,
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual([row["status"] for row in results], ["ok", "ok", "unavailable"])
        self.assertIsNone(results[0]["source_event_time_ms"])
        self.assertEqual(results[1]["source_event_time_ms"], target + duration - 7)
        self.assertEqual(results[2]["reason"], "target_candle_not_complete")

    def test_missing_configuration_fails_closed(self):
        app = create_app("")
        self.assertEqual(self.request(app, "GET", "/health").status_code, 503)
        self.assertEqual(
            self.request(app, "POST", "/v1/analysis", json=payload(), headers=HEADERS).status_code,
            503)

    def test_rolling_and_baseline_use_different_comparisons(self):
        body = payload()
        original = deepcopy(body)
        response = self.post(body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual(data["mode"], "read_only")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(Decimal(data["rolling"]["15"]["change_pct"]), 0)
        self.assertEqual(Decimal(data["baseline"]["change_pct"]), 2)
        self.assertEqual(data["baseline"]["status"], "eligible")
        self.assertTrue(data["baseline"]["alert_eligible"])
        self.assertTrue(data["baseline"]["cooldown_evaluated"])
        self.assertEqual(set(data["technical"]), {"15", "60", "240"})
        self.assertTrue(all(row["status"] == "ok" for row in data["technical"].values()))
        self.assertEqual(data["source_instrument"]["instrument_id"],
                         "binance-usdm:BTCUSDT")
        self.assertTrue(all(row["factor_breakdown"] for row in data["technical"].values()))
        self.assertEqual(body, original)

    def test_missing_and_changed_baseline_are_not_reset(self):
        for baseline, expected in ((None, "baseline_required"),
                                   ({**payload()["baseline"], "threshold": "3"}, "baseline_reset_required")):
            with self.subTest(expected=expected):
                data = self.post({**payload(), "baseline": baseline}).json()["baseline"]
                self.assertEqual(data["status"], expected)
                self.assertFalse(data["eligibility_evaluated"])
                self.assertIsNone(data["alert_eligible"])

    def test_directional_cooldown_and_exact_expiry(self):
        body = payload()
        body["baseline"]["last_up_alert_ms"] = NOW - 5 * MINUTE
        data = self.post(body).json()["baseline"]
        self.assertEqual(data["status"], "cooldown")
        self.assertFalse(data["alert_eligible"])
        self.assertEqual(data["cooldown_until_ms"], NOW + 10 * MINUTE)
        body["baseline"]["last_up_alert_ms"] = NOW - 15 * MINUTE
        self.assertTrue(self.post(body).json()["baseline"]["alert_eligible"])

    def test_downward_reversal_ignores_upward_cooldown(self):
        body = payload()
        body["baseline"]["last_up_alert_ms"] = NOW - MINUTE
        async def falling(client, provider, symbol):
            return workload(provider, symbol, "98")
        data = self.post(body, falling).json()["baseline"]
        self.assertEqual(data["direction"], "down")
        self.assertEqual(Decimal(data["change_pct"]), -2)
        self.assertTrue(data["alert_eligible"])

    def test_already_processed_disabled_and_below_threshold(self):
        body = payload()
        body["baseline"]["last_observed_ms"] = NOW
        data = self.post(body).json()["baseline"]
        self.assertEqual(data["status"], "already_processed")
        self.assertFalse(data["alert_eligible"])
        body["settings"]["monitoring_enabled"] = False
        self.assertEqual(self.post(body).json()["baseline"]["status"], "disabled")
        async def below(client, provider, symbol):
            return workload(provider, symbol, "101.99")
        data = self.post(loader=below).json()["baseline"]
        self.assertEqual(data["status"], "below_threshold")
        self.assertFalse(data["cooldown_evaluated"])

    def test_partial_history_preserves_independent_baseline_analysis(self):
        async def partial(client, provider, symbol):
            value = workload(provider, symbol)
            value["rolling"][15] = value["rolling"][15][-2:]
            return value
        data = self.post(loader=partial).json()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["rolling"]["1440"]["status"], "unavailable")
        self.assertTrue(data["baseline"]["alert_eligible"])

    def test_insufficient_ta_history_is_explicit(self):
        async def insufficient(client, provider, symbol):
            value = workload(provider, symbol)
            value["technical"][60] = value["technical"][60][-10:]
            return value
        data = self.post(loader=insufficient).json()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["failure_category"], "insufficient_history")
        self.assertEqual(data["technical"]["60"]["status"], "insufficient")
        self.assertEqual(data["technical"]["60"]["reason"], "insufficient_history")

    def test_stale_data_and_provider_errors_have_no_eligibility(self):
        async def stale(client, provider, symbol):
            value = workload(provider, symbol)
            value["rolling"] = {
                interval: [Candle(c.open_ms - 20 * MINUTE, c.close, c.complete)
                           for c in candles]
                for interval, candles in value["rolling"].items()
            }
            return value
        data = self.post(loader=stale).json()
        self.assertEqual(data["status"], "unavailable")
        self.assertFalse(data["baseline"]["eligibility_evaluated"])
        self.assertIsNone(data["baseline"]["alert_eligible"])
        self.assertEqual(len(data["attempts"]), len(ANALYSIS_PROVIDERS))

    def test_provider_failure_redacts_errors(self):
        async def unavailable(client, provider, symbol):
            raise httpx.ConnectError("private-provider-details")
        response = self.post(loader=unavailable)
        self.assertIsNone(response.json()["source"])
        self.assertEqual(response.json()["attempts"],
                         [{"source": source, "reason": "provider_unavailable"}
                          for source in ANALYSIS_PROVIDERS])
        self.assertNotIn("private-provider-details", response.text)

    def test_validation_rejects_unknown_fields_symbols_and_bad_state(self):
        bodies = [{**payload(), "symbol": "UNKNOWNUSDT"},
                  {**payload(), "instrument_id": "binance-usdm:ETHUSDT"},
                  {**payload(), "user_id": "private-user"}]
        for value in ("0", "NaN", "Infinity", "101"):
            bodies.append({**payload(), "settings": {**payload()["settings"], "threshold_pct": value}})
        for value in (0, 1441, 1.5, True):
            bodies.append({**payload(), "settings": {**payload()["settings"], "cooldown_minutes": value}})
        bodies.append({**payload(), "baseline": {**payload()["baseline"], "at_ms": NOW + MINUTE}})
        for body in bodies:
            with self.subTest(body=body):
                response = self.post(body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid analysis request"})

    def test_timeout_and_unexpected_failure_are_sanitized(self):
        async def slow(*args):
            await asyncio.sleep(1)
        async def broken(*args):
            raise RuntimeError("private-token-content")
        for analyzer, expected in ((slow, 504), (broken, 502)):
            app = create_app(TOKEN, analyzer, analysis_timeout=0.01)
            response = self.request(app, "POST", "/v1/analysis",
                                    json=payload(), headers=HEADERS)
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("private-token-content", response.text)

    def test_async_http_adapter_reuses_provider_conventions(self):
        async def check():
            urls = []
            def handler(request):
                urls.append(str(request.url))
                if request.url.path.endswith("/exchangeInfo"):
                    return httpx.Response(200, json={"symbols": [{
                        "symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL",
                        "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
                        "marginAsset": "USDT", "onboardDate": 1569398400000,
                        "deliveryDate": 4133404800000, "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]})
                interval = 1 if request.url.params["interval"] == "1m" else 15
                row = [NOW - interval * MINUTE, "100", "102", "100", "102", "1", NOW - 1]
                return httpx.Response(200, json=[row])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                loaded = await load_series(client, SOURCE, "BTCUSDT")
            self.assertEqual(loaded[1][0].close, Decimal("102"))
            self.assertTrue(any("limit=62" in url for url in urls))
            self.assertTrue(any("limit=98" in url for url in urls))
        asyncio.run(check())

    def test_workload_adapter_fetches_all_ta_frames_from_one_provider(self):
        async def check():
            intervals = []
            def handler(request):
                if request.url.path.endswith("/exchangeInfo"):
                    return httpx.Response(200, json={"symbols": [{
                        "symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL",
                        "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT",
                        "marginAsset": "USDT", "onboardDate": 1569398400000,
                        "deliveryDate": 4133404800000, "filters": [
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                            {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]})
                text = request.url.params["interval"]
                interval = int(text[:-1]) * (60 if text.endswith("h") else 1)
                intervals.append((interval, request.url.params["limit"]))
                opened = NOW - interval * MINUTE
                return httpx.Response(200, json=[
                    [opened, "100", "102", "99", "101", "10",
                     opened + interval * MINUTE - 1]
                ])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                loaded = await load_workload(client, SOURCE, "BTCUSDT")
            self.assertEqual(intervals, [(1, "62"), (15, "250"), (60, "250"), (240, "250")])
            self.assertEqual(set(loaded["technical"]), {15, 60, 240})
            self.assertEqual(loaded["source_instrument"]["native_symbol"], "BTCUSDT")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
