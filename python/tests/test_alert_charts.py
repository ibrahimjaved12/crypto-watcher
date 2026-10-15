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
from market_analysis_tools import fixtures as fx

PYTHON_DIR = Path(__file__).resolve().parents[1]
HAS_MPL = importlib.util.find_spec("matplotlib") is not None
REQUIRE_MPL = os.environ.get("ALERT_CHARTS_REQUIRE_MATPLOTLIB") == "1"
needs_mpl = unittest.skipUnless(HAS_MPL or REQUIRE_MPL, "matplotlib is a machine-side requirement")
PNG = b"\x89PNG\r\n\x1a\n"
START = fx.START
synthetic_candles, pattern_window, movement_window, trade_fixture = (
    fx.synthetic_candles, fx.pattern_window, fx.movement_window, fx.trade_fixture)


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


class LayoutTests(unittest.TestCase):
    """The label layout is pure (no matplotlib): anchors are never closer than the minimum gap."""

    def gaps(self, ys):
        values = sorted(ys.values())
        return [b - a for a, b in zip(values, values[1:])]

    def test_spread_labels_keeps_order_range_and_gap(self):
        ys = ac.spread_labels({"a": 100.0, "b": 100.001, "c": 100.002, "d": 90.0}, 80.0, 101.0, 2.0)
        self.assertEqual(sorted(ys, key=ys.get), ["d", "a", "b", "c"])
        self.assertTrue(all(g >= 2.0 - 1e-9 for g in self.gaps(ys)))
        self.assertTrue(all(80.0 <= y <= 101.0 for y in ys.values()))
        far = ac.spread_labels({"a": 1.0, "b": 9.0}, 0.0, 10.0, 2.0)
        self.assertEqual(far, {"a": 1.0, "b": 9.0})  # already apart: unchanged
        with self.assertRaises(ac.WindowError):
            ac.spread_labels({"a": 1, "b": 2, "c": 3}, 0.0, 1.0, 2.0)

    def test_movement_labels_with_close_baseline_and_alert_never_collide(self):
        for close in (True, False):
            layout = ac.movement_layout(movement_window(close=close))
            gap = layout["min_gap"]
            lo, hi = layout["ylim"]
            self.assertAlmostEqual(gap, ac.MIN_LABEL_GAP * (hi - lo))
            self.assertGreaterEqual(abs(layout["label_y"]["alert"] - layout["label_y"]["baseline"]), gap - 1e-9)
            self.assertTrue(lo <= min(layout["label_y"].values()) and max(layout["label_y"].values()) <= hi)
        close = ac.movement_layout(movement_window(close=True))
        self.assertLess(abs(close["anchors"]["alert"] - close["anchors"]["baseline"]), close["min_gap"])  # the real case
        self.assertIn("threshold 0.2%", close["alert_text"])  # one merged label; no separate threshold label
        self.assertNotIn("Threshold", close["alert_text"] + close["baseline_text"])

    def test_the_timing_note_is_computed_and_never_claims_adjacency(self):
        window = movement_window(minutes_after=7)
        self.assertEqual(ac.alert_gap_minutes(window), 7)
        note = ac.movement_layout(window)["gap_note"]
        self.assertIn("7 min after the last completed 15m candle closed", note)
        self.assertIn("not adjacent", note)
        self.assertEqual(ac.alert_gap_minutes(movement_window(minutes_after=0)), 0)

    def test_trade_labels_with_close_levels_never_collide(self):
        window = pattern_window(50)
        layout = ac.trade_layout(window, trade_fixture(window, close_levels=True))
        values = sorted(layout["label_y"].values())
        self.assertEqual(len(values), 4)
        self.assertTrue(all(b - a >= layout["min_gap"] - 1e-9 for a, b in zip(values, values[1:])))
        lo, hi = layout["ylim"]
        self.assertTrue(lo <= values[0] and values[-1] <= hi)
        with self.assertRaises(ac.WindowError):
            ac.trade_layout(window, {**trade_fixture(window), "side": "long"})


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
