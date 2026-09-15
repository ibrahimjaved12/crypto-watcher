import asyncio
from copy import deepcopy
from decimal import Decimal
import unittest

from fastapi.testclient import TestClient
import httpx

from market_analysis.api import create_app
from market_analysis.core import Candle, MINUTE
from market_analysis.service import analyze_request, load_series

NOW = 1704153600000
TOKEN = "test-service-token-" + "x" * 32
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def payload():
    return {"schema_version": 1, "symbol": "BTCUSDT",
            "settings": {"threshold_pct": "2", "cooldown_minutes": 15, "monitoring_enabled": True},
            "baseline": {"price": "100", "at_ms": NOW - 60 * MINUTE,
                         "source": "Binance", "threshold": "2",
                         "last_observed_ms": NOW - 5 * MINUTE,
                         "last_up_alert_ms": None, "last_down_alert_ms": None}}


def series(price="102"):
    return {interval: [Candle(NOW - n * interval * MINUTE, Decimal(price))
                       for n in range(count, -1, -1)]
            for interval, count in ((1, 61), (15, 97))}


async def fixture_loader(*args):
    return series()


def analyzer_for(loader=fixture_loader):
    async def analyzer(request, client):
        return await analyze_request(request, client, loader=loader, clock=lambda: NOW)
    return analyzer


class ApiTests(unittest.TestCase):
    def post(self, body=None, loader=fixture_loader):
        with TestClient(create_app(TOKEN, analyzer_for(loader))) as client:
            return client.post("/v1/analysis", json=body or payload(), headers=HEADERS)

    def test_health_and_authentication(self):
        with TestClient(create_app(TOKEN, analyzer_for())) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic abc"}):
                response = client.post("/v1/analysis", json=payload(), headers=headers)
                self.assertEqual(response.status_code, 401)
            self.assertEqual(client.post("/v1/analysis", json=payload(), headers=HEADERS).status_code, 200)

    def test_missing_configuration_fails_closed(self):
        with TestClient(create_app("")) as client:
            self.assertEqual(client.get("/health").status_code, 503)
            self.assertEqual(client.post("/v1/analysis", json=payload(), headers=HEADERS).status_code, 503)

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
        self.assertEqual(body, original)

    def test_missing_and_changed_baseline_are_not_reset(self):
        for baseline, expected in ((None, "baseline_required"),
                                   ({**payload()["baseline"], "source": "OKX"}, "baseline_reset_required"),
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
        async def falling(*args):
            return series("98")
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
        async def below(*args):
            return series("101.99")
        data = self.post(loader=below).json()["baseline"]
        self.assertEqual(data["status"], "below_threshold")
        self.assertFalse(data["cooldown_evaluated"])

    def test_partial_history_preserves_independent_baseline_analysis(self):
        async def partial(*args):
            value = series()
            value[15] = value[15][-2:]
            return value
        data = self.post(loader=partial).json()
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["rolling"]["1440"]["status"], "unavailable")
        self.assertTrue(data["baseline"]["alert_eligible"])

    def test_stale_data_and_provider_errors_have_no_eligibility(self):
        async def stale(*args):
            return {interval: [Candle(c.open_ms - 20 * MINUTE, c.close, c.complete)
                               for c in candles] for interval, candles in series().items()}
        data = self.post(loader=stale).json()
        self.assertEqual(data["status"], "unavailable")
        self.assertFalse(data["baseline"]["eligibility_evaluated"])
        self.assertIsNone(data["baseline"]["alert_eligible"])
        self.assertEqual(len(data["attempts"]), 3)

    def test_provider_fallback_and_redacted_errors(self):
        async def fallback(client, provider, symbol):
            if provider == "Binance":
                raise httpx.ConnectError("private-provider-details")
            return series()
        response = self.post(loader=fallback)
        self.assertEqual(response.json()["source"], "OKX")
        self.assertNotIn("private-provider-details", response.text)

    def test_validation_rejects_unknown_fields_symbols_and_bad_state(self):
        bodies = [{**payload(), "symbol": "UNKNOWNUSDT"}, {**payload(), "user_id": "private-user"}]
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
            with TestClient(create_app(TOKEN, analyzer, analysis_timeout=0.01)) as client:
                response = client.post("/v1/analysis", json=payload(), headers=HEADERS)
                self.assertEqual(response.status_code, expected)
                self.assertNotIn("private-token-content", response.text)

    def test_async_http_adapter_reuses_provider_conventions(self):
        async def check():
            urls = []
            def handler(request):
                urls.append(str(request.url))
                interval = 1 if request.url.params["interval"] == "1m" else 15
                row = [NOW - interval * MINUTE, "100", "102", "100", "102", "1", NOW - 1]
                return httpx.Response(200, json=[row])
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                loaded = await load_series(client, "Binance", "BTCUSDT")
            self.assertEqual(loaded[1][0].close, Decimal("102"))
            self.assertTrue(any("limit=62" in url for url in urls))
            self.assertTrue(any("limit=98" in url for url in urls))
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
