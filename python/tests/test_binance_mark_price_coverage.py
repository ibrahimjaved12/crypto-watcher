"""Generated local archive fixtures for the separate mark-price coverage audit."""

import csv
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from market_analysis import historical_experiment_batch as batch
from market_analysis.binance_mark_price_coverage import (
    MARK_PRICE_WARMUP_MINUTES, build_mark_price_coverage_report,
    daily_mark_price_relative_path, mark_price_coverage_report_to_json,
)


MINUTE = 60_000
START = int(datetime(2026, 8, 20, 12, 20, tzinfo=timezone.utc).timestamp() * 1000)
END = START + 4 * MINUTE
DAY = datetime(2026, 8, 20, tzinfo=timezone.utc).date()


def _row(opening, *, open_price="100", high="102", low="99", close="101"):
    return [str(opening), open_price, high, low, close, "0",
            str(opening + MINUTE - 1), "0", "0", "0", "0", "0"]


def _write_archive(root: Path, symbol: str, rows, *, checksum=True):
    relative = daily_mark_price_relative_path(symbol, DAY)
    archive = root.joinpath(*relative.parts)
    archive.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerows(rows)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"{relative.stem}.csv", text.getvalue())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if checksum:
        Path(f"{archive}.CHECKSUM").write_text(
            f"{digest}  {relative.name}\n", encoding="ascii")
    return archive, digest


def _expected_rows():
    warmup_start = START - MARK_PRICE_WARMUP_MINUTES * MINUTE
    return [_row(opening) for opening in range(warmup_start, END, MINUTE)]


def _report(root, symbols=("BTCUSDT",), *, start=START, end=END):
    return build_mark_price_coverage_report(
        tuple(symbols), start, end, root, code_revision="fixture-revision")


class BinanceMarkPriceCoverageTests(unittest.TestCase):
    def test_verified_checksum_and_complete_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_archive(root, "BTCUSDT", _expected_rows())
            report = _report(root)
            symbol = report["period_coverage"][0]
            package = symbol["packages"][0]
            self.assertTrue(package["archive_present"])
            self.assertTrue(package["checksum_present"])
            self.assertTrue(package["checksum_verified"])
            self.assertEqual(symbol["coverage_ratio"], 1.0)
            self.assertEqual(package["status"], "VALID_COMPLETE")
            self.assertEqual(report["identity"]["effective_warmup"]["minutes"], 16)
            self.assertEqual(symbol["contiguous_ready_coverage"]["15m"]
                             ["ready_boundary_count"], 4)
            self.assertEqual(mark_price_coverage_report_to_json(report),
                             mark_price_coverage_report_to_json(report))

    def test_checksum_mismatch_is_reported_without_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive, _ = _write_archive(root, "BTCUSDT", _expected_rows())
            Path(f"{archive}.CHECKSUM").write_text(f"{'0' * 64}  {archive.name}\n",
                                                   encoding="ascii")
            package = _report(root)["period_coverage"][0]["packages"][0]
            self.assertIn("CHECKSUM_MISMATCH", package["statuses"])
            self.assertFalse(package["checksum_verified"])
            self.assertEqual(package["row_count"], 0)

    def test_invalid_schema_row_is_separate_from_missing_minutes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = _expected_rows()
            rows[2] = rows[2][:-1]
            _write_archive(root, "BTCUSDT", rows)
            package = _report(root)["period_coverage"][0]["packages"][0]
            self.assertIn("INVALID_SCHEMA_ROW", package["statuses"])
            self.assertIn("VALID_WITH_MISSING_MINUTES", package["statuses"])
            self.assertEqual(package["parse_error_count"], 1)
            self.assertEqual(package["missing_minute_count"], 1)

    def test_gaps_duplicates_and_off_grid_rows_are_reported_separately(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = _expected_rows()
            rows = [row for row in rows if int(row[0]) != START - 8 * MINUTE]
            rows.append(next(row for row in _expected_rows()
                             if int(row[0]) == START - 7 * MINUTE))
            rows.append(_row(START + 30_000))
            _write_archive(root, "BTCUSDT", rows)
            package = _report(root)["period_coverage"][0]["packages"][0]
            self.assertEqual(package["duplicate_row_count"], 1)
            self.assertEqual(package["off_grid_row_count"], 1)
            self.assertEqual(package["missing_minute_count"], 1)
            self.assertIn("DUPLICATE_TIMESTAMP", package["statuses"])
            self.assertIn("OFF_GRID_TIMESTAMP", package["statuses"])
            self.assertIn("VALID_WITH_MISSING_MINUTES", package["statuses"])

    def test_symbol_order_is_bound_to_audit_identity_and_core_suite_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for symbol in ("BTCUSDT", "ETHUSDT"):
                _write_archive(root, symbol, _expected_rows())
            suite_before = tuple((item.experiment_id, item.algorithm_version,
                                  item.config_version)
                                 for item in batch.EXPERIMENT_SUITE_V1)
            self.assertEqual(len(suite_before), 28)
            forward = _report(root, ("BTCUSDT", "ETHUSDT"))
            reverse = _report(root, ("ETHUSDT", "BTCUSDT"))
            self.assertNotEqual(forward["audit_sha256"], reverse["audit_sha256"])
            self.assertEqual(forward["identity"]["ordered_symbols"],
                             ["BTCUSDT", "ETHUSDT"])
            self.assertEqual(reverse["identity"]["ordered_symbols"],
                             ["ETHUSDT", "BTCUSDT"])
            self.assertEqual(tuple((item.experiment_id, item.algorithm_version,
                                    item.config_version)
                                   for item in batch.EXPERIMENT_SUITE_V1), suite_before)
            self.assertEqual(len(batch.EXPERIMENT_SUITE_V1), 28)
            self.assertNotIn("dataset_content_sha256", forward["identity"])
            self.assertNotIn("replay_fingerprint", forward["identity"])
            self.assertNotIn("point_stream_sha256", forward["identity"])

    def test_missing_package_and_missing_checksum_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = _report(root)["period_coverage"][0]["packages"][0]
            self.assertIn("MISSING_PACKAGE", missing["statuses"])
            self.assertIn("MISSING_CHECKSUM", missing["statuses"])
            _write_archive(root, "BTCUSDT", _expected_rows(), checksum=False)
            missing_checksum = _report(root)["period_coverage"][0]["packages"][0]
            self.assertEqual(missing_checksum["status"], "MISSING_CHECKSUM")


if __name__ == "__main__":
    unittest.main()
