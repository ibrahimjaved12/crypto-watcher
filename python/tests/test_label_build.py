"""Pure planning helpers; no private API calls or label builds."""
from contextlib import redirect_stderr
import importlib.util
from array import array
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis import data_lake
from market_analysis.benchmark import label_cli
from market_analysis.benchmark.bars import BarSeries
from market_analysis.benchmark.funding import read_funding_csv

from market_analysis.data_lake import month_bounds_ms


def load_script():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    definition = importlib.util.spec_from_file_location("test_label_build_script", scripts / "label_build.py")
    module = importlib.util.module_from_spec(definition)
    previous_path = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


class LabelBuildHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load_script()
        cls.now_ms = month_bounds_ms("2026-10")[0]

    def test_tag_naming_and_validation(self):
        tag = self.builder.label_tag("BTCUSDT", "2024-01", "2026-09")
        self.assertEqual(tag, "lb1-BTCUSDT-2024-01_2026-09-r1")
        self.assertEqual(self.builder.validate_label_tag(tag), tag)
        self.assertEqual(self.builder.label_tag("XRPUSDT", "2024-01", "2024-01", 12),
                         "lb1-XRPUSDT-2024-01_2024-01-r12")
        for invalid in (tag.replace("lb1-", "rd-"), tag.replace("BTCUSDT", "OTHER"),
                        tag.replace("r1", "r01"), tag.replace("2024-01", "2024-13"),
                        tag.replace("2024-01", "2027-01"), tag + "/bad"):
            with self.subTest(tag=invalid), self.assertRaises(ValueError):
                self.builder.validate_label_tag(invalid)

    def test_inclusive_month_planning_and_finished_boundary(self):
        self.assertEqual(self.builder.plan_months("2024-12", "2025-02"), ["2024-12", "2025-01", "2025-02"])
        months = self.builder.plan_months("2024-01", "2026-09", now_ms=self.now_ms)
        self.assertEqual(len(months), 33)
        self.assertEqual(months[-1], "2026-09")
        with self.assertRaises(ValueError):
            self.builder.plan_months("2026-09", "2026-09", now_ms=self.now_ms - 1)
        for first, last in (("2023-12", "2024-01"), ("2025-02", "2025-01"), ("2024-1", "2024-02")):
            with self.assertRaises(ValueError):
                self.builder.plan_months(first, last)

    def test_symbol_list_and_revision_validation(self):
        self.assertEqual(self.builder.validate_symbols("BTCUSDT,ETHUSDT"), ["BTCUSDT", "ETHUSDT"])
        for raw in ("", "BTCUSDT, BTCUSDT", "BTCUSDT,BTCUSDT", "OTHER", "btcusdt", "BTCUSDT,"):
            with self.assertRaises(ValueError):
                self.builder.validate_symbols(raw)
        for raw in ("0", "01", "1000", "-1", "1.0", " 1"):
            with self.assertRaises(ValueError):
                self.builder.revision(raw)
        self.assertEqual(self.builder.revision("999"), 999)

    def test_arguments(self):
        args = self.builder.parse_args(["--symbol", "BTCUSDT"], now_ms=self.now_ms)
        self.assertFalse(args.publish)
        self.assertEqual((args.data_revision, args.label_revision), (1, 1))
        self.assertEqual((args.first_month, args.last_month), ("2024-01", "2026-09"))
        args = self.builder.parse_args(["--symbol", "BTCUSDT", "--publish", "--data-revision", "2",
                                        "--label-revision", "3"], now_ms=self.now_ms)
        self.assertTrue(args.publish)
        self.assertEqual((args.data_revision, args.label_revision), (2, 3))
        for options in (["--symbol", "OTHER"], ["--data-revision", "0"], ["--label-revision", "01"],
                        ["--first-month", "2023-01"], ["--last-month", "2026-10"]):
            with self.subTest(options=options), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.builder.parse_args(["--symbol", "BTCUSDT", *options], now_ms=self.now_ms)


def tick_series(month, off_prints):
    start = month_bounds_ms(month)[0]
    prices = array("q", ((517_350 + i % 1000) * 10 ** 7 for i in range(3000)))
    columns = {name: array("q", prices) for name in ("open", "high", "low", "close", "mark_open", "mark_high",
                                                     "mark_low", "mark_close")}
    columns.update({name: array("q", [0] * 3000) for name in ("volume", "taker_buy_volume", "trades")})
    for index in range(off_prints):
        columns["high"][index] = 5_173_562 * 10 ** 6
    return BarSeries("BTCUSDT", start, 3000, flags=array("H", [0] * 3000), **columns)


class TickManifestTests(unittest.TestCase):
    def test_manifest_records_off_tick_prints_and_rule_without_logging_ticks(self):
        month = "2025-02"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (data_lake.bars_asset_name("BTCUSDT", month), data_lake.funding_asset_name("BTCUSDT", month)):
                (root / name).write_bytes(b"synthetic")
            empty_funding = ",".join(data_lake.FUNDING_COLUMNS) + "\n"
            log = io.StringIO()
            with patch.object(label_cli, "read_bars_csv", lambda stream, symbol, m: tick_series(m, 1)), \
                    patch.object(label_cli, "read_funding_csv",
                                 lambda stream, m: read_funding_csv(io.StringIO(empty_funding), m)), \
                    patch.object(label_cli, "build_labels", lambda *args: []), redirect_stdout(log):
                manifest = label_cli.run("BTCUSDT", root, month, month, root / "out")
            written = json.loads((root / "out" / label_cli.manifest_name("BTCUSDT")).read_text())
        for record in (manifest, written):
            self.assertEqual(record["tick_rule"], "tick-v2")
            self.assertEqual(record["off_tick_prints"], {month: 1})
            self.assertEqual(record["ticks"], {month: 10 ** 7})
        self.assertNotIn(str(10 ** 7), log.getvalue())
        self.assertIn("1 months with off-tick prints, 0 month-to-month tick changes", log.getvalue())

    def test_public_tick_aggregates_and_release_line(self):
        builder = load_script()
        manifest = {"ticks": {"2024-01": 10 ** 7, "2024-02": 10 ** 7, "2024-03": 2 * 10 ** 6, "2024-04": 10 ** 7},
                    "off_tick_prints": {"2024-01": 0, "2024-02": 1, "2024-03": 0, "2024-04": 4},
                    "tick_rule": "tick-v2", "params": {"rr_grid": ["1"]}, "outputs": [],
                    "schema": "labels-v1", "params_identity": "p", "cost_model_identity": "c",
                    "symbol": "BTCUSDT", "rd_tags": ["rd-BTCUSDT-2024-01-r1"]}
        self.assertEqual(label_cli.tick_summary(manifest), {"months_with_off_tick_prints": 2, "tick_changes": 2})
        counts = builder.aggregate_counts(manifest, 3)
        self.assertEqual((counts["months_with_off_tick_prints"], counts["tick_changes"], counts["tick_rule"]),
                         (2, 2, "tick-v2"))
        self.assertNotIn(str(10 ** 7), json.dumps(counts))
        self.assertIn("Tick rule: tick-v2 (outlier-tolerant)", builder.release_body(manifest))
        old = {**manifest, "tick_rule": "tick-v1"}
        self.assertNotIn("Tick rule", builder.release_body(old))
        self.assertNotIn(str(10 ** 7), builder.release_body(manifest))


if __name__ == "__main__":
    unittest.main()
