"""Alert setup chart renderer (market_analysis_tools.alert_charts, #299).

Every candle and number here is SYNTHETIC (a seeded random walk), labelled so in the windows
(``synthetic: true`` prints the synthetic note on the chart). Validation tests need no matplotlib;
the rendering tests skip without it in the default CI job and run in .github/workflows/alert-charts.yml
(ALERT_CHARTS_REQUIRE_MATPLOTLIB=1 turns a missing matplotlib into a failure there).
"""
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest

from market_analysis_tools import alert_charts as ac

PYTHON_DIR = Path(__file__).resolve().parents[1]
HAS_MPL = importlib.util.find_spec("matplotlib") is not None
REQUIRE_MPL = os.environ.get("ALERT_CHARTS_REQUIRE_MATPLOTLIB") == "1"
needs_mpl = unittest.skipUnless(HAS_MPL or REQUIRE_MPL, "matplotlib is a machine-side requirement")
PNG = b"\x89PNG\r\n\x1a\n"
START = 1_767_225_600_000  # 2026-01-01 00:00 UTC


def synthetic_candles(n=60, timeframe=15, seed=7, doji_at=None):
    """SYNTHETIC seeded random-walk candles [open_ms, o, h, l, c, v]; not market data."""
    rng = random.Random(seed)
    price, rows = 100.0, []
    for i in range(n):
        o = price
        c = o + rng.gauss(0.02, 0.7)
        if i == doji_at:
            c = o + 0.02
        h = max(o, c) + abs(rng.gauss(0.18, 0.08)) + 0.01 + (0.5 if i == doji_at else 0)
        l = min(o, c) - abs(rng.gauss(0.18, 0.08)) - 0.01 - (0.5 if i == doji_at else 0)
        rows.append([START + i * timeframe * 60_000, round(o, 4), round(h, 4), round(l, 4), round(c, 4),
                     round(abs(rng.gauss(100, 35)), 2)])
        price = c
    return rows


def ema(values, period):
    out = [None] * len(values)
    if len(values) >= period:
        prev = sum(values[:period]) / period
        out[period - 1] = prev
        for i in range(period, len(values)):
            prev = (values[i] - prev) * 2 / (period + 1) + prev
            out[i] = prev
    return out


def pattern_window(n=60):
    candles = synthetic_candles(n, 60, doji_at=n - 1)
    closes = [c[4] for c in candles]
    return {"version": ac.WINDOW_VERSION, "kind": "pattern", "synthetic": True, "symbol": "BTCUSDT",
            "source": "binance-usdm", "timeframe_minutes": 60, "event_candle_ms": candles[-1][0],
            "patterns": ["doji"], "rule_version": "ta-v2", "candles": candles,
            "indicators": {"ema20": ema(closes, 20), "ema50": ema(closes, 50),
                           "rsi14": [None] * 14 + [50.0] * (n - 14)}}


def movement_window(n=96):
    candles = synthetic_candles(n, 15)
    last = candles[-1]
    alert_ms = last[0] + 15 * 60_000 + 3 * 60_000
    baseline = candles[40][4]
    price = round(baseline * 1.021, 4)
    return {"version": ac.WINDOW_VERSION, "kind": "movement_alert", "synthetic": True, "symbol": "ETHUSDT",
            "source": "binance-usdm", "timeframe_minutes": 15, "event_candle_ms": last[0], "candles": candles,
            "indicators": {"ema20": [None] * n, "ema50": [None] * n, "rsi14": [None] * n},
            "alert": {"alert_id": "00000000-0000-4000-8000-000000000001", "rule_version": "cumulative-baseline-v1",
                      "direction": "up", "threshold_pct": 2, "change_pct": 2.1, "baseline_price": baseline,
                      "baseline_ms": candles[40][0], "alert_price": price, "alert_ms": alert_ms,
                      "alert_candle_ms": alert_ms - 60_000,
                      "alert_minute": [alert_ms - 60_000, last[4], max(last[4], price) + 0.1,
                                       min(last[4], price) - 0.1, price, 12.0]},
            "breadth": {"status": "LIVE", "window_minutes": 5, "evaluation_boundary_ms": alert_ms,
                        "direction_state": "RISING", "denominator": 6, "rising": 5, "falling": 0, "flat": 1,
                        "material_rising": 4, "material_falling": 0},
            "breadth_unavailable_reason": None}


class ValidationTests(unittest.TestCase):
    def test_a_candle_after_the_event_candle_is_refused(self):
        window = pattern_window()
        window["event_candle_ms"] = window["candles"][-2][0]
        with self.assertRaisesRegex(ac.WindowError, "after the event candle"):
            ac.validate_candles(window)

    def test_malformed_candles_are_refused(self):
        for bad in ([[START, 1, 0.5, 0.4, 1, 1]], [[START, 1, 2, 0.5, 1, -1]], [[START, 1, 2, 0.5]], []):
            with self.assertRaises(ac.WindowError):
                ac.validate_candles({"candles": bad, "event_candle_ms": START})
        out_of_order = synthetic_candles(3)
        out_of_order[1], out_of_order[2] = out_of_order[2], out_of_order[1]
        with self.assertRaises(ac.WindowError):
            ac.validate_candles({"candles": out_of_order, "event_candle_ms": out_of_order[1][0]})

    def test_window_kind_and_version_are_checked(self):
        with self.assertRaises(ac.WindowError):
            ac.render_window({**pattern_window(), "version": "v0"}, "/tmp/never.png")
        with self.assertRaises(ac.WindowError):
            ac.render_window({**pattern_window(), "kind": "other"}, "/tmp/never.png")

    def test_the_tool_package_does_not_import_matplotlib_or_enter_the_core(self):
        code = "import sys, market_analysis_tools.alert_charts; print('matplotlib' in sys.modules)"
        out = subprocess.run([sys.executable, "-c", code], cwd=PYTHON_DIR, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")
        core = PYTHON_DIR / "market_analysis"
        offenders = [p.name for p in core.rglob("*.py") if "matplotlib" in p.read_text(encoding="utf-8")
                     or "market_analysis_tools" in p.read_text(encoding="utf-8")]
        self.assertEqual(offenders, [])


@needs_mpl
class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_png(self, path):
        data = Path(path).read_bytes()
        self.assertTrue(data.startswith(PNG))
        self.assertGreater(len(data), 10_000)
        return data

    def test_pattern_snapshot_is_a_png_and_deterministic(self):
        a, b = self.dir / "a.png", self.dir / "b.png"
        ac.render_pattern_snapshot(pattern_window(), None, a)
        ac.render_pattern_snapshot(pattern_window(), None, b)
        self.assertEqual(self.assert_png(a), b.read_bytes())

    def test_movement_alert_chart(self):
        path = self.dir / "alert.png"
        ac.render_alert_chart(movement_window(), None, path)
        self.assert_png(path)
        without_breadth = {**movement_window(), "breadth": None, "breadth_unavailable_reason": "movement_state_stale"}
        ac.render_alert_chart(without_breadth, None, self.dir / "alert2.png")
        self.assert_png(self.dir / "alert2.png")

    def test_trade_overlay_and_equity(self):
        window = pattern_window(50)
        entry = window["candles"][22]
        ac.render_trade_overlay(window, {"side": "short", "entry_ms": entry[0], "entry": entry[4],
                                         "stop": entry[4] + 1.4, "target": entry[4] - 2.8,
                                         "liquidation": entry[4] + 3.6}, self.dir / "trade.png")
        self.assert_png(self.dir / "trade.png")
        with self.assertRaises(ac.WindowError):
            ac.render_trade_overlay(window, {"side": "long", "entry_ms": entry[0], "entry": entry[4],
                                             "stop": entry[4] + 1, "target": entry[4] + 2}, self.dir / "bad.png")
        rng = random.Random(3)
        equity, value = [], 100.0
        for _ in range(60):
            value += rng.gauss(0.05, 0.9)
            equity.append(round(value, 4))
        ac.render_equity({"synthetic": True, "equity": equity, "closes": [5, 9, 20, 33, 50],
                          "score": [rng.uniform(-100, 100) for _ in range(60)]}, self.dir / "equity.png")
        self.assert_png(self.dir / "equity.png")

    def test_command_line_render_reports_the_digest(self):
        path = self.dir / "cli.png"
        job = json.dumps({"window": movement_window(), "path": str(path)})
        out = subprocess.run([sys.executable, "-m", "market_analysis_tools.alert_charts", "render"], input=job,
                             cwd=PYTHON_DIR, capture_output=True, text=True, check=True)
        digest = json.loads(out.stdout)
        self.assertEqual(digest, ac.file_digest(path))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["cli.png"])  # no temporary file left
        bad = json.dumps({"window": {**movement_window(), "kind": "x"}, "path": str(self.dir / "x.png")})
        failed = subprocess.run([sys.executable, "-m", "market_analysis_tools.alert_charts", "render"], input=bad,
                                cwd=PYTHON_DIR, capture_output=True, text=True)
        self.assertEqual(failed.returncode, 1)
        self.assertIn("WindowError", failed.stderr)
        self.assertFalse((self.dir / "x.png").exists())


if __name__ == "__main__":
    unittest.main()
