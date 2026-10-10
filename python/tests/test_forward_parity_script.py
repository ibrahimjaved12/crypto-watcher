"""Parity workflow report persistence, without private data or network access."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from market_analysis.forward.outcomes import Resolution
from market_analysis.benchmark.canonical import canonical_bytes, content_hash
from test_forward_parity import TICK, reference_row, resolution, setup_dict


def load_script():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    definition = importlib.util.spec_from_file_location("test_forward_parity_script", scripts / "forward_parity.py")
    module = importlib.util.module_from_spec(definition)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


class ParityReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def test_run_writes_and_pushes_hashed_report_with_and_without_slice_check(self):
        # Exercise the actual comparison, hash, file write and public verdict path. Only the
        # expensive bar/sigma evaluation and private repository operations are replaced.
        for slice_enabled, slice_identical, geometry_identical in (
                (False, True, True), (True, True, True), (True, False, True), (False, True, False)):
            with self.subTest(slice=slice_enabled, identical=slice_identical, geometry=geometry_identical), \
                    TemporaryDirectory() as directory, ExitStack() as patches:
                script = self.script
                args = script.parse_args(["--symbol", "BTCUSDT", "--month", "2025-05"])
                args.slice_check = slice_enabled
                start = script.lake.month_bounds_ms(args.month)[0]
                setup = setup_dict(signal_ms=start + 5 * script.MINUTE)
                if not geometry_identical:
                    setup["sigma"] = "1200"
                ref = reference_row(setup, d_ticks=40 if geometry_identical else 39)
                reference = Mock(return_value=ref)
                reference.robust = SimpleNamespace(level_at=lambda *a: 500, multiplier=lambda *a: 1_000_000)
                reference.tick_at = lambda *a: TICK
                result = {"signals": [{"signal_id": "signal"}], "setups": [setup],
                          "resolutions": [resolution(setup)]}
                slice_result = {"hours": script.SLICE_HOURS, "signals": 1, "setups": 1,
                                "identical": slice_identical}
                replacements = (
                    (script.xr, "_download_symbol_bars", None),
                    (script, "load_symbol_bars", object()),
                    (script, "load_symbol_funding", object()),
                    (script, "infer_tick", TICK),
                    (script.lab, "row_factory", reference),
                    (script, "run_forward", result),
                    (script, "request", {"symbols": [{"rows": [], "funding": []}]}),
                    (script, "bars_from_collector_rows", SimpleNamespace(symbol="BTCUSDT")),
                    (script, "_funding", None),
                    (script.parity.outcomes, "resolve_setup", Resolution(**resolution(setup))),
                    (script, "published_cross_check", {"revision": None}),
                )
                for target, name, returned in replacements:
                    patches.enter_context(patch.object(target, name, return_value=returned))
                slice_mock = patches.enter_context(patch.object(script, "slice_check", return_value=slice_result))
                checkout = Mock(path=Path(directory) / "research-data")
                lines = script.run(args, checkout, Mock(), Path(directory) / "data", MagicMock())
                paths, message = checkout.commit_and_push.call_args.args
                self.assertEqual(len(paths), 1)
                raw = (checkout.path / paths[0]).read_bytes()
                report = json.loads(raw)
                report_hash = report.pop("report_hash")
                self.assertEqual(report_hash, content_hash(report))
                self.assertEqual(raw, canonical_bytes({**report, "report_hash": report_hash}))
                self.assertIn(report_hash[:16], paths[0])
                self.assertIn(report_hash, message)
                self.assertEqual(lines[-1], f"report hash {report_hash}")
                self.assertEqual(report["pass_rule"], {
                    "unmatched_max": 0, "identical_geometry_status_exit_share_min": "1.0",
                    "geometry_or_sigma_share_min": "0.95", "sigma_tolerance": "0.03",
                    "median_c_h_tolerance": "0.03",
                    "reference_geometry_status_exit_net_share_min": "1.0"})
                self.assertEqual(report["stats"]["abs_rel_delta_sigma"]["median"],
                                 "0.0" if geometry_identical else "0.2")
                self.assertEqual(report["stats"]["verdict"]["pass"], geometry_identical)
                self.assertEqual(args.failed, not (geometry_identical and slice_identical))
                self.assertIn("PASS" if geometry_identical else "FAIL", lines[0])
                if slice_enabled:
                    slice_mock.assert_called_once()
                    self.assertEqual(report["slice_check"], slice_result)
                    self.assertTrue(lines[1].endswith("PASS" if slice_identical else "FAIL"))
                else:
                    slice_mock.assert_not_called()
                    self.assertNotIn("slice_check", report)

    def test_report_rendering_preserves_precision_and_handles_undefined_statistics(self):
        original = {"values": (1 / 3, float("inf"), float("-inf"), float("nan"), None, 7, True),
                    "nested": [{"delta": 0.000000000123456789}]}
        rendered = self.script.report_record(original)
        self.assertEqual(rendered, {"values": [repr(1 / 3), None, None, None, None, 7, True],
                                    "nested": [{"delta": "1.23456789e-10"}]})
        self.assertEqual(float(rendered["values"][0]), original["values"][0])
        self.assertIsInstance(original["nested"][0]["delta"], float)
        canonical_bytes(rendered)
        # The shared serializer must continue to reject raw float values.
        with self.assertRaises(TypeError):
            canonical_bytes(original)


class SettlementResolutionTests(unittest.TestCase):
    def test_rule_v_uses_the_same_settlement_mark_as_evaluate(self):
        from market_analysis.forward.bars_adapter import bars_from_collector_rows, apply_funding_marks
        from market_analysis.forward.evaluate import evaluate, _funding
        from market_analysis.forward.parity import reference_setup
        from market_analysis.benchmark.scan import label_trade
        from market_analysis.forward.setups import setup_id
        from fractions import Fraction
        from dataclasses import replace

        script = load_script()
        start = script.lake.month_bounds_ms("2025-05")[0]
        rows = [{"open_time_ms": start + i * script.MINUTE, "open": 100.0, "high": 100.01,
                 "low": 99.99, "close": 100.0, "transport": "rest"} for i in range(120)]
        events = [{"calc_time_ms": start + 60 * script.MINUTE, "rate": "0.001", "mark": "110"}]
        item = {"symbol": "BTCUSDT", "rows": rows, "funding": events}
        geometry = setup_dict(signal_ms=start + 45 * script.MINUTE, p0=100 * 10**8, d_ticks=200)
        reference = reference_row(geometry, d_ticks=200)
        setup = reference_setup(geometry, reference, lambda _: TICK, script.FORWARD_PARAMS, "BTCUSDT")
        setup = replace(setup, setup_id=setup_id(setup.signal_id, Fraction(2), Fraction(2), setup.params_hash))
        geometry["setup_id"] = setup.setup_id
        engine = evaluate([item], strategy_ids=[], from_ms=start, to_ms=start + 119 * script.MINUTE,
                          open_setups=[{"setup": setup.to_dict()}])
        bars = bars_from_collector_rows("BTCUSDT", rows)
        funding = _funding(bars, events)
        unmarked = script.parity.outcomes.resolve_setup(setup, bars, funding)
        apply_funding_marks(bars, events)
        expected = script.parity.outcomes.resolve_setup(setup, bars, funding)
        self.assertEqual(engine["resolutions"], [expected.to_dict()])
        self.assertNotEqual(unmarked.net_ur, expected.net_ur)
        label = label_trade(bars, funding, script.FORWARD_PARAMS.cost_model, tick=TICK,
                            entry_index=45, side=1, stop_price=setup.stop,
                            target_prices=[setup.target], window_minutes=setup.window_minutes)
        reference.cells = (label.cells[0], label.cells[0])
        ref = Mock(return_value=reference)
        ref.robust = SimpleNamespace(level_at=lambda *a: 500, multiplier=lambda *a: 1_000_000)
        ref.tick_at = lambda *a: TICK
        with TemporaryDirectory() as directory, ExitStack() as patches:
            for target, name, value in (
                (script.xr, "_download_symbol_bars", None), (script, "load_symbol_bars", bars),
                (script, "load_symbol_funding", funding), (script, "infer_tick", TICK),
                (script.lab, "row_factory", ref), (script, "request", {"symbols": [item]}),
                (script, "run_forward", {**engine, "setups": [geometry]}),
                (script, "published_cross_check", {"revision": None}),
            ):
                patches.enter_context(patch.object(target, name, return_value=value))
            args = script.parse_args(["--symbol", "BTCUSDT", "--month", "2025-05"])
            checkout = Mock(path=Path(directory) / "repo")
            script.run(args, checkout, Mock(), Path(directory) / "data", MagicMock())
            [path] = checkout.commit_and_push.call_args.args[0]
            stats = json.loads((checkout.path / path).read_text())["stats"]
            self.assertTrue(stats["verdict"]["v_reference_geometry_agrees"])
            self.assertEqual(stats["reference_geometry_status_exit_net_ok"], 1)


if __name__ == "__main__":
    unittest.main()
