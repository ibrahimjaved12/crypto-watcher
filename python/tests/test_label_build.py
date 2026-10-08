"""Pure planning helpers; no private API calls or label builds."""
from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
