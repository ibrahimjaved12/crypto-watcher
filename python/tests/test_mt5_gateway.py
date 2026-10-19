"""MT5 gateway (#452): offset detection, refusals, UTC conversion, auth, limits and the safety scan.

The MT5 module is a FAKE with SYNTHETIC rates; nothing here talks to a terminal or a broker.
"""

from datetime import datetime, timezone
from fractions import Fraction
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stderr
import tempfile
import threading
import unittest
from unittest import mock

from market_analysis.fx.bars import FLAG_PRESENT, fx_bars_from_rows
from market_analysis.instruments import VenueSymbol, forex_instrument
from market_analysis.trading_calendar import FX_WEEK

GATEWAY_PATH = Path(__file__).resolve().parents[2] / "mt5" / "gateway" / "mt5_gateway.py"
_spec = importlib.util.spec_from_file_location("mt5_gateway", GATEWAY_PATH)
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

TOKEN = "t" * 40
AUTH = "Bearer " + TOKEN
OFFSET = 10_800  # UTC+3 broker server, SYNTHETIC
SYMBOL = "EURUSDm"
NOW = int(datetime(2026, 10, 14, 12, 30, 30, tzinfo=timezone.utc).timestamp())  # a Wednesday, market open
MINUTE_S = NOW - NOW % 60  # the forming minute (12:30)


class Clock:
    def __init__(self, now=float(NOW)):
        self.now = now

    def __call__(self):
        return self.now


def rate(utc_s, offset=OFFSET):
    return {"time": utc_s + offset, "open": 1.08, "high": 1.0802, "low": 1.0799, "close": 1.0801,
            "tick_volume": 7, "spread": 12, "real_volume": 0}


class FakeMt5:
    TIMEFRAME_M1 = 1

    def __init__(self, clock, offset=OFFSET, rates=None, init=True):
        self.clock, self.offset, self.init, self.rates = clock, offset, init, rates if rates is not None else []
        self.calls = []
        self.known = {SYMBOL, "XAUUSDm"}
        self.live = True
        self.stale_tick = 0

    def initialize(self, *args, **kwargs):
        self.calls.append(("initialize", args, kwargs))
        return self.init

    def terminal_info(self):
        return SimpleNamespace(connected=self.init)

    def symbols_get(self):
        return [SimpleNamespace(name=name) for name in sorted(self.known)]

    def symbol_select(self, name, enable):
        self.calls.append(("symbol_select", name, enable))
        return name in self.known

    def symbol_info(self, name):
        if name not in self.known:
            return None
        return SimpleNamespace(digits=5, point=0.00001, trade_tick_size=0.00001, trade_tick_value=1.0,
                               trade_contract_size=100000.0, volume_min=0.01, volume_max=200.0, volume_step=0.01,
                               currency_base="EUR", currency_profit="USD", currency_margin="EUR", trade_mode=4,
                               swap_mode=1, swap_long=-7.3, swap_short=2.1, swap_rollover3days=3,
                               secret_extra="never exposed")

    def symbol_info_tick(self, name):
        if name not in self.known:
            return None
        return SimpleNamespace(time=self.stale_tick or int(self.clock()) + self.offset)

    def copy_rates_range(self, name, timeframe, first, last):
        self.calls.append(("copy_rates_range", name, timeframe, first, last))
        return [r for r in self.rates if first <= r["time"] <= last]  # inclusive both ends: the worst case


def make(tmp, **kwargs):
    clock = kwargs.pop("clock", None) or Clock()
    mt5 = kwargs.pop("mt5", None) or FakeMt5(clock)
    kwargs.setdefault("state_path", os.path.join(tmp, "state.json"))
    gateway = gw.Gateway(mt5, clock=clock, token=TOKEN, **kwargs)
    return gateway, mt5, clock


def get(gateway, target, auth=AUTH, method="GET"):
    return gateway.handle(method, target, auth)


def bars_target(from_s, to_s, symbol=SYMBOL):
    return "/v1/bars?symbol=%s&from_ms=%d&to_ms=%d" % (symbol, from_s * 1000, to_s * 1000)


class OffsetRuleTests(unittest.TestCase):
    def test_rounding_and_residual(self):
        self.assertEqual(gw.rounded_offset(10_800), 10_800)
        self.assertEqual(gw.rounded_offset(10_800 + 59), 10_800)
        self.assertEqual(gw.rounded_offset(10_800 - 60), 10_800)
        self.assertIsNone(gw.rounded_offset(10_800 + 61))
        self.assertIsNone(gw.rounded_offset(10_800 + 450))  # halfway between two quarter hours
        self.assertEqual(gw.rounded_offset(-3_600 + 30), -3_600)
        self.assertEqual(gw.rounded_offset(0), 0)
        self.assertEqual(gw.rounded_offset(19_800 + 20), 19_800)  # UTC+5:30
        self.assertIsNone(gw.rounded_offset(15 * 3600))  # not a civil offset
        self.assertIsNone(gw.rounded_offset(-15 * 3600))

    def test_detection_needs_a_fresh_changing_tick_and_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, mt5, clock = make(tmp)
            self.assertFalse(gateway.offset_known())
            gateway.maybe_detect(SYMBOL)  # first reading: nothing to compare with yet
            self.assertEqual(gateway.store.observations, [])
            self.assertIs(gateway.market_open_hint, False)
            clock.now += 30
            gateway.maybe_detect(SYMBOL)  # throttled: at most once a minute
            self.assertEqual(gateway.store.observations, [])
            clock.now += 31
            gateway.maybe_detect(SYMBOL)
            self.assertEqual(gateway.store.observations, [{"observed_at_ms": int(clock.now * 1000), "offset_seconds": OFFSET}])
            self.assertTrue(gateway.offset_known())
            self.assertIs(gateway.market_open_hint, True)
            # A tick that does not change is a stale feed, even when the residual rule passes by chance.
            mt5.stale_tick = int(clock.now) + OFFSET
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            self.assertEqual(len(gateway.store.observations), 1)  # an unchanged tick time is not a live feed
            # A tick far from "now" fails the residual rule.
            mt5.stale_tick = 0
            mt5.offset = OFFSET + 450
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            self.assertEqual(len(gateway.store.observations), 1)
            self.assertIs(gateway.market_open_hint, False)

    def test_override_is_used_and_never_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, mt5, clock = make(tmp, offset_override_seconds=OFFSET)
            clock.now += 120
            gateway.maybe_detect(SYMBOL)
            self.assertEqual(gateway.store.observations, [])
            self.assertEqual(gateway.resolve_offset(0, 10**12), (OFFSET, "override"))
            self.assertTrue(gateway.offset_known())
            report = get(gateway, "/v1/clock")[1]
            self.assertEqual((report["server_offset_seconds"], report["method"], report["observed_at_ms"]),
                             (OFFSET, "override", None))
            for bad in (OFFSET + 1, 15 * 3600):
                with self.assertRaises(ValueError):
                    gw.Gateway(mt5, clock=clock, offset_override_seconds=bad)


class BarsTests(unittest.TestCase):
    def rates(self, first_minute_s, count):
        return [rate(first_minute_s + 60 * i) for i in range(count)]

    def test_a_forming_minute_is_excluded_and_broker_time_becomes_utc(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            mt5 = FakeMt5(clock, rates=self.rates(MINUTE_S - 300, 6))  # 12:25 .. 12:30, 12:30 is forming
            gateway, _, _ = make(tmp, clock=clock, mt5=mt5, offset_override_seconds=OFFSET)
            status, body = get(gateway, bars_target(MINUTE_S - 300, MINUTE_S + 60))
            self.assertEqual(status, 200)
            self.assertEqual([row["open_time_ms"] for row in body["bars"]],
                             [(MINUTE_S - 300 + 60 * i) * 1000 for i in range(5)])
            self.assertEqual((body["offset_seconds"], body["offset_method"]), (OFFSET, "override"))
            first = body["bars"][0]
            self.assertEqual(set(first), {"time_basis", "native_symbol", "open_time_ms", "open", "high", "low", "close",
                                          "spread_points", "tick_volume"})
            self.assertEqual((first["time_basis"], first["native_symbol"], first["spread_points"], first["tick_volume"]),
                             ("utc", SYMBOL, 12, 7))
            self.assertIsInstance(first["spread_points"], int)
            # MT5 was asked in broker time, and only Market Watch selection was used.
            asked = [call for call in mt5.calls if call[0] == "copy_rates_range"][0]
            self.assertEqual(asked[1:], (SYMBOL, 1, MINUTE_S - 300 + OFFSET, MINUTE_S + 60 + OFFSET))
            self.assertIn(("symbol_select", SYMBOL, True), mt5.calls)
            # The minute becomes available once it is closed.
            clock.now = MINUTE_S + 61
            self.assertEqual(len(get(gateway, bars_target(MINUTE_S - 300, MINUTE_S + 60))[1]["bars"]), 6)

    def test_the_range_is_half_open_even_if_mt5_returns_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            mt5 = FakeMt5(clock, rates=self.rates(MINUTE_S - 300, 6))
            gateway, _, _ = make(tmp, clock=clock, mt5=mt5, offset_override_seconds=OFFSET)
            body = get(gateway, bars_target(MINUTE_S - 240, MINUTE_S - 120))[1]
            self.assertEqual([row["open_time_ms"] // 1000 for row in body["bars"]], [MINUTE_S - 240, MINUTE_S - 180])

    def test_the_offset_is_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            mt5 = FakeMt5(clock, rates=self.rates(MINUTE_S - 300, 6))
            gateway, _, _ = make(tmp, clock=clock, mt5=mt5)
            target = bars_target(MINUTE_S - 300, MINUTE_S)
            self.assertEqual(get(gateway, target), (503, {"error": "offset_unknown"}))
            self.assertFalse([call for call in mt5.calls if call[0] == "copy_rates_range"])
            # Two agreeing detections inside the range: served.
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            wide = bars_target(NOW - 3600, int(clock.now))
            self.assertEqual(get(gateway, wide)[0], 200)
            self.assertEqual(get(gateway, wide)[1]["offset_method"], "detected")
            # The broker moves its clock (daylight saving time): observations in the range disagree.
            mt5.offset = OFFSET - 3600
            clock.now += 61
            gateway.maybe_detect(SYMBOL)
            self.assertEqual({o["offset_seconds"] for o in gateway.store.observations}, {OFFSET, OFFSET - 3600})
            self.assertEqual(get(gateway, bars_target(NOW - 3600, int(clock.now))), (409, {"error": "offset_changed"}))
            # A range after the change that holds only the new observation is served with the new offset.
            self.assertEqual(get(gateway, bars_target(int(clock.now) - 30, int(clock.now) + 30))[1]["offset_seconds"],
                             OFFSET - 3600)

    def test_range_symbol_and_parameter_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, mt5, clock = make(tmp, offset_override_seconds=OFFSET)
            start = MINUTE_S - 6000 * 60
            self.assertEqual(get(gateway, bars_target(start, start + 5000 * 60))[0], 200)
            self.assertEqual(get(gateway, bars_target(start, start + 5001 * 60)), (400, {"error": "range_too_large"}))
            for target in ("/v1/bars", "/v1/bars?symbol=%s&from_ms=x&to_ms=2" % SYMBOL,
                           "/v1/bars?symbol=%s&from_ms=5&to_ms=5" % SYMBOL,
                           "/v1/bars?symbol=%s&from_ms=6&to_ms=5" % SYMBOL,
                           "/v1/bars?symbol=EUR USD&from_ms=1&to_ms=2",
                           "/v1/bars?symbol=%s&from_ms=-1&to_ms=2" % SYMBOL):
                self.assertEqual(get(gateway, target), (400, {"error": "bad_request"}), target)
            self.assertEqual(get(gateway, bars_target(1, 2, symbol="NOPE")), (404, {"error": "unknown_symbol"}))

    def test_rows_pass_through_fx_bars_from_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            noon = MINUTE_S - 30 * 60  # 12:00
            mt5 = FakeMt5(clock, rates=self.rates(noon, 31))
            gateway, _, _ = make(tmp, clock=clock, mt5=mt5, offset_override_seconds=OFFSET)
            body = get(gateway, bars_target(noon, MINUTE_S))[1]
            self.assertEqual(len(body["bars"]), 30)
            venue = VenueSymbol("test-venue", SYMBOL, "fx:EURUSD", digits=5, tick_size=Fraction(1, 100_000))
            # The collector adds venue and transport; the gateway's rows are otherwise exactly what the checker takes.
            rows = [dict(row, venue="test-venue", transport="history") for row in body["bars"]]
            series = fx_bars_from_rows(forex_instrument("EURUSD"), venue, FX_WEEK, rows)
            self.assertEqual(series.minutes, 30)
            self.assertEqual(series.start_ms, noon * 1000)
            self.assertTrue(all(flag == FLAG_PRESENT for flag in series.flags))
            self.assertEqual(series.spread[0], 12 * 10 ** 3)  # 12 points of 1e-5 at 10**8


class SpecsAndSymbolsTests(unittest.TestCase):
    def test_specs_fields_and_null_for_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, _, _ = make(tmp)
            status, body = get(gateway, "/v1/specs?symbols=%s,NOPE" % SYMBOL)
            self.assertEqual(status, 200)
            self.assertIsNone(body["specs"]["NOPE"])
            spec = body["specs"][SYMBOL]
            self.assertEqual(list(spec), list(gw.SPEC_FIELDS))
            self.assertNotIn("secret_extra", spec)
            self.assertEqual((spec["digits"], spec["point"], spec["trade_tick_size"], spec["trade_contract_size"],
                              spec["volume_min"], spec["currency_base"], spec["currency_profit"], spec["swap_mode"],
                              spec["swap_long"], spec["swap_rollover3days"]),
                             (5, 0.00001, 0.00001, 100000.0, 0.01, "EUR", "USD", 1, -7.3, 3))
            for bad in ("/v1/specs", "/v1/specs?symbols=", "/v1/specs?symbols=A,A", "/v1/specs?symbols=bad%20name",
                        "/v1/specs?symbols=" + ",".join("S%d" % i for i in range(51))):
                self.assertEqual(get(gateway, bad), (400, {"error": "bad_symbols"}), bad)

    def test_symbols_clock_and_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, mt5, clock = make(tmp)
            self.assertEqual(get(gateway, "/v1/symbols"), (200, {"symbols": ["EURUSDm", "XAUUSDm"]}))
            report = get(gateway, "/v1/clock")[1]
            self.assertEqual(set(report), {"now_utc_ms", "server_offset_seconds", "method", "observed_at_ms",
                                           "market_open_hint"})
            self.assertEqual((report["now_utc_ms"], report["server_offset_seconds"], report["method"]), (NOW * 1000, None, None))
            health = gateway.handle("GET", "/health", None)
            self.assertEqual(health[0], 200)
            self.assertEqual(set(health[1]), {"ok", "terminal_connected", "offset_known", "now_utc_ms"})
            self.assertEqual((health[1]["ok"], health[1]["terminal_connected"], health[1]["offset_known"]), (True, True, False))

    def test_terminal_not_initialized_is_reported_and_never_retried_with_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            mt5 = FakeMt5(clock, init=False)
            gateway, _, _ = make(tmp, clock=clock, mt5=mt5)
            health = gateway.handle("GET", "/health", None)[1]
            self.assertEqual((health["ok"], health["terminal_connected"]), (False, False))
            self.assertEqual(get(gateway, bars_target(1, 2)), (503, {"error": "terminal_unavailable"}))
            clock.now += 31
            self.assertEqual(get(gateway, "/v1/symbols")[0], 503)
            initializations = [call for call in mt5.calls if call[0] == "initialize"]
            self.assertEqual(len(initializations), 2)  # at construction and one throttled retry
            self.assertTrue(all(call[1:] == ((), {}) for call in initializations), "never any credentials")


class HttpTests(unittest.TestCase):
    def test_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, _, _ = make(tmp)
            for path in ("/v1/clock", "/v1/symbols", "/v1/specs?symbols=A", "/v1/bars?symbol=A&from_ms=1&to_ms=2",
                         "/v1/unknown"):
                for header in (None, "", "Bearer", "Bearer wrong", "bearer " + TOKEN, TOKEN, "Basic " + TOKEN,
                               "Bearer " + TOKEN + "x", "Bearer " + "é" * 5):
                    self.assertEqual(get(gateway, path, auth=header), (401, {"error": "unauthorized"}), (path, header))
            self.assertEqual(get(gateway, "/v1/unknown")[0], 404)
            self.assertEqual(get(gateway, "/health", auth=None)[0], 200)
            self.assertEqual(get(gateway, "/v1/clock", method="POST")[0], 405)
            self.assertEqual(get(gateway, "/v1/clock?" + "a" * 1100)[0], 414)
            self.assertEqual(get(gateway, "/" + "a" * 2100)[0], 414)

    def test_over_a_real_socket_no_cors_and_token_not_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, _, _ = make(tmp)
            server = gw.ThreadingHTTPServer(("127.0.0.1", 0), gw.make_handler(gateway))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            log = io.StringIO()
            try:
                with redirect_stderr(log):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
                    connection.request("GET", "/v1/symbols")
                    denied = connection.getresponse()
                    denied.read()
                    connection.request("GET", "/v1/symbols", headers={"Authorization": AUTH})
                    ok = connection.getresponse()
                    body = json.loads(ok.read())
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
            self.assertEqual((denied.status, denied.getheader("WWW-Authenticate")), (401, "Bearer"))
            self.assertEqual((ok.status, ok.getheader("Content-Type")), (200, "application/json"))
            self.assertEqual(body, {"symbols": ["EURUSDm", "XAUUSDm"]})
            for response in (denied, ok):
                self.assertFalse([name for name, _ in response.getheaders() if name.lower().startswith("access-control")])
            self.assertNotIn(TOKEN, log.getvalue())


class StateFileTests(unittest.TestCase):
    def test_written_atomically_and_reloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            store = gw.OffsetStore(path)
            store.add(1_000, OFFSET)
            self.assertEqual(json.load(open(path))["observations"], [{"observed_at_ms": 1_000, "offset_seconds": OFFSET}])
            self.assertEqual(os.listdir(tmp), ["state.json"], "no temporary file is left behind")
            self.assertEqual(gw.OffsetStore(path).observations, store.observations)
            # A failure while replacing leaves the old file untouched and cleans up.
            with mock.patch.object(gw.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    store.add(2_000, OFFSET)
            self.assertEqual(os.listdir(tmp), ["state.json"])
            self.assertEqual(len(json.load(open(path))["observations"]), 1)
            # The file is bounded.
            for index in range(gw.MAX_OBSERVATIONS + 10):
                store.observations.append({"observed_at_ms": 10 + index, "offset_seconds": OFFSET})
            store.add(10**9, OFFSET)
            self.assertEqual(len(json.load(open(path))["observations"]), gw.MAX_OBSERVATIONS)
            # A corrupt file means "nothing observed".
            with open(path, "w") as handle:
                handle.write("{not json")
            self.assertEqual(gw.OffsetStore(path).observations, [])


class SafetyAndConfigTests(unittest.TestCase):
    def test_the_source_has_no_trading_or_account_calls(self):
        source = GATEWAY_PATH.read_text(encoding="utf-8")
        for name in ("order_send", "order_check", "positions_get", "orders_get", "history_deals_get", "account_info",
                     "login"):
            self.assertNotIn(name, source, name)

    def test_the_source_is_python_39_syntax(self):
        import ast
        tree = ast.parse(GATEWAY_PATH.read_text(encoding="utf-8"), feature_version=(3, 9))
        self.assertFalse([node for node in ast.walk(tree) if type(node).__name__ == "Match"])

    def test_token_and_main_configuration(self):
        with self.assertRaises(ValueError):
            gw.Gateway(FakeMt5(Clock()), token="short")
        error = io.StringIO()
        with redirect_stderr(error):
            self.assertEqual(gw.main([], environ={}), 2)
            self.assertEqual(gw.main([], environ={"MT5_GATEWAY_TOKEN": "bad token with spaces " * 3}), 2)
            self.assertEqual(gw.main([], environ={"MT5_GATEWAY_TOKEN": TOKEN, "MT5_GATEWAY_PORT": "x"}), 2)
            self.assertEqual(gw.main([], environ={"MT5_GATEWAY_TOKEN": TOKEN, "MT5_SERVER_UTC_OFFSET_SECONDS": "1.5"}), 2)
        self.assertNotIn(TOKEN, error.getvalue())


if __name__ == "__main__":
    unittest.main()
