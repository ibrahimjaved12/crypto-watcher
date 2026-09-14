import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from market_analysis.core import Candle, MINUTE, analyze, analyze_window, percentage_change, threshold_met
from market_analysis.providers import parse, request_url
from market_analysis.__main__ import run

FIXTURE = json.loads((Path(__file__).parent / "fixtures/candles.json").read_text())
AS_OF = FIXTURE["as_of_ms"]
CANDLES = [Candle(t, Decimal(p)) for t, p in FIXTURE["minute"]]


def full_series():
    # Fixed UTC grid, enough closed history for all five windows, plus a live bar.
    end = 1704153600000
    return {i: [Candle(end - n * i * MINUTE, Decimal("100"))
                for n in range(count, -1, -1)]
            for i, count in ((1, 61), (15, 97))}


class CalculationTests(unittest.TestCase):
    def window(self, candles=CANDLES, as_of=AS_OF):
        return analyze_window(candles, 5, as_of, "2")

    def test_completed_upward_boundary(self):
        value = self.window()
        self.assertEqual(value["change_pct"], "2.00")
        self.assertTrue(value["threshold_met"])
        self.assertEqual(value["end_close_ms"] - value["start_close_ms"], 5 * MINUTE)

    def test_downward_boundary(self):
        candles = CANDLES.copy()
        candles[5] = replace(candles[5], close=Decimal("98"))
        value = self.window(candles)
        self.assertEqual(Decimal(value["change_pct"]), -2)
        self.assertTrue(value["threshold_met"])

    def test_decimal_threshold_boundaries(self):
        for price, expected in (("101.999999", False), ("102", True), ("102.000001", True),
                                ("98.000001", False), ("98", True), ("97.999999", True),
                                ("100", False)):
            with self.subTest(price=price):
                self.assertEqual(threshold_met(percentage_change("100", price), "2"), expected)

    def test_missing_baseline(self):
        self.assertEqual(self.window(CANDLES[1:])["reason"], "missing_candles")

    def test_internal_gap_does_not_shift_window(self):
        self.assertEqual(self.window(CANDLES[:2] + CANDLES[3:])["reason"], "missing_candles")

    def test_incomplete_extreme_price_ignored(self):
        self.assertEqual(self.window()["change_pct"], self.window(CANDLES[:-1])["change_pct"])
        # Legacy last-vs-five-rows-back computes ~49.7%, not the completed 2%.
        legacy = percentage_change(CANDLES[-6].close, CANDLES[-1].close)
        self.assertGreater(legacy, 49)

    def test_close_boundary(self):
        self.assertEqual(self.window(as_of=1704067560000)["status"], "ok")
        self.assertEqual(self.window(CANDLES[:-1], as_of=1704067559999)["reason"], "missing_candles")
        self.assertEqual(self.window(as_of=1704067559999)["reason"], "invalid_or_future_timestamp")

    def test_threshold_comparison_does_not_round_to_default_decimal_precision(self):
        self.assertFalse(threshold_met("1.999999999999999999999999999999", "2"))
        self.assertFalse(threshold_met("-1.999999999999999999999999999999", "2"))

    def test_stale_boundary(self):
        candles = CANDLES[:-1]
        close = candles[-1].open_ms + MINUTE
        self.assertEqual(self.window(candles, close + 10 * MINUTE)["status"], "ok")
        self.assertEqual(self.window(candles, close + 11 * MINUTE)["status"], "stale")

    def test_future_and_misaligned(self):
        for timestamp in (AS_OF + MINUTE, CANDLES[0].open_ms + 1):
            self.assertEqual(self.window([Candle(timestamp, Decimal(100))])["reason"],
                             "invalid_or_future_timestamp")

    def test_duplicate(self):
        self.assertEqual(self.window(CANDLES + CANDLES[:1])["reason"], "duplicate_timestamp")

    def test_invalid_prices(self):
        for price in ("0", "-1", "NaN", "Infinity", "oops"):
            with self.subTest(price=price):
                with self.assertRaises(ValueError):
                    percentage_change(price, "100")
                self.assertEqual(self.window([Candle(CANDLES[0].open_ms, price)])["reason"],
                                 "invalid_price")

    def test_invalid_threshold(self):
        for threshold in ("0", "-2", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                threshold_met("2", threshold)

    def test_empty_and_unconfirmed(self):
        self.assertEqual(self.window([])["reason"], "no_completed_candles")
        self.assertEqual(self.window([replace(c, complete=False) for c in CANDLES])["reason"],
                         "no_completed_candles")

    def test_all_windows_and_quarter_freshness(self):
        values = analyze(full_series(), 1704153600000, "2")
        for window, value in values.items():
            self.assertEqual(value["status"], "ok")
            self.assertEqual(value["end_close_ms"] - value["start_close_ms"], int(window) * MINUTE)
        quarter = analyze_window(full_series()[15], 240, 1704153600000 + 14 * MINUTE, "2")
        self.assertEqual(quarter["status"], "ok")

    def test_quarter_stale_independent_of_minute(self):
        series = full_series()
        series[15] = series[15][:-2]
        values = analyze(series, 1704153600000, "2")
        self.assertEqual(values["5"]["status"], "ok")
        self.assertEqual(values["240"]["status"], "stale")

    def test_order_independent(self):
        self.assertEqual(self.window(), self.window(list(reversed(CANDLES))))


class ProviderTests(unittest.TestCase):
    def test_timestamp_units_and_completion(self):
        for source in ("Binance", "OKX", "Kraken"):
            rows = parse(source, FIXTURE[source.lower()], 1)
            self.assertEqual(rows[0].open_ms, 1704067200000)
            self.assertEqual(rows[0].close, Decimal(100))
            self.assertTrue(rows[0].complete)
            if source != "Binance":
                self.assertFalse(rows[-1].complete)

    def test_symbol_and_extra_history_conventions(self):
        for symbol in ("BTCUSDT", "DOGEUSDT"):
            for source, key, expected in (("Binance", "symbol", symbol),
                                          ("OKX", "instId", symbol[:-4] + "-USDT"),
                                          ("Kraken", "pair", symbol)):
                params = parse_qs(urlsplit(request_url(source, symbol, 15)).query)
                self.assertEqual(params[key], [expected])
                if source != "Kraken":
                    self.assertEqual(params["limit"], ["98"])

    def test_api_errors(self):
        for source, body in (("Binance", []), ("OKX", {"code": "1", "msg": "bad pair"}),
                             ("Kraken", {"error": ["Unknown asset pair"]})):
            with self.assertRaises(ValueError):
                parse(source, body, 1)

    def test_wrong_binance_close_time(self):
        row = FIXTURE["binance"][0].copy()
        row[6] += 1
        with self.assertRaises(ValueError):
            parse("Binance", [row], 1)

    def test_fallback_order_no_mixing(self):
        calls = []
        def loader(source, symbol):
            calls.append(source)
            if source == "Binance":
                raise URLError("fixture outage")
            return full_series()
        value = run("BTCUSDT", "2", 15, loader, lambda: 1704153600000)
        self.assertEqual(calls, ["Binance", "OKX"])
        self.assertEqual(value["source"], "OKX")
        self.assertTrue(value["ok"])
        self.assertFalse(value["cooldown_checked"])
        json.dumps(value, allow_nan=False)

    def test_stale_falls_back_and_total_failure_is_structured(self):
        value = run("BTCUSDT", "2", 15, lambda *args: full_series(),
                    lambda: 1704153600000 + 30 * MINUTE)
        self.assertFalse(value["ok"])
        self.assertEqual([a["source"] for a in value["attempts"]], ["Binance", "OKX", "Kraken"])
        self.assertEqual(value["attempts"][0]["windows"]["5"]["status"], "stale")


if __name__ == "__main__":
    unittest.main()
