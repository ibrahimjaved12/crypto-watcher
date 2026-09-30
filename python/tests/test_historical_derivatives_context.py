"""Generated source archives only; no external data or acquisition in these fixtures."""
import csv
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import gzip
import hashlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from market_analysis import historical_open_interest_evidence as oi
from market_analysis import historical_open_interest_extension as oix
from market_analysis import historical_funding_evidence as funding
from market_analysis import historical_funding_extension as fx
from market_analysis import historical_liquidation_evidence as liq
from market_analysis import historical_liquidation_extension as lx
from market_analysis import historical_experiment_batch as batch
from market_analysis.binance_historical_archive import (
    BinanceUSDMArchiveRequest, load_binance_usdm_historical_replay_dataset,
)
from market_analysis.historical_replay import (
    run_historical_market_replay, to_market_state_experiment_points,
)
from market_analysis.movement_metrics import Metric
from test_historical_experiment_batch import _request, OUTPUT, SYMBOLS

MINUTE = 60_000
DAY = date(2026, 9, 1)
BASE = (datetime(2026, 9, 1, tzinfo=timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)
T = BASE + 30 * MINUTE


def _zip(root, relative, header, rows, checksum=True):
    path = root.joinpath(*relative.parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    csv.writer(text).writerows([header, *rows])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(relative.stem + ".csv", text.getvalue())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if checksum:
        Path(str(path) + ".CHECKSUM").write_text(f"{digest}  {relative.name}\n")
    return path


def _oi(root, symbol="BTCUSDT", rows=None, header=None, checksum=True, day=DAY):
    if rows is None:
        rows = [(BASE + m * MINUTE, symbol, str(100 + m), "1000") for m in (10, 15, 20, 25, 30)]
    return _zip(root, oi.daily_open_interest_relative_path(symbol, day),
                header or ("create_time", "symbol", "sum_open_interest", "sum_open_interest_value"), rows, checksum)


def _funding(root, symbol="BTCUSDT", rows=None, month=DAY, checksum=True, header=None):
    return _zip(root, funding.monthly_funding_relative_path(symbol, month),
        header or ("calc_time", "funding_interval_hours", "last_funding_rate"),
        rows if rows is not None else [(BASE, 1, "-0.0002")], checksum)


def _gzip(root, rows, day=DAY, header=liq.FIELDS):
    path = root.joinpath(*liq.daily_liquidation_relative_path(day).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    csv.writer(text).writerows([header, *rows])
    path.write_bytes(gzip.compress(text.getvalue().encode(), mtime=0))
    return path


def _snapshot(event, receipt=None, symbol="BTCUSDT", side="sell", price="100", amount="2"):
    return ("binance-futures", symbol, event, event if receipt is None else receipt, "id", side, price, amount)


def _point(boundary=T, symbols=("BTCUSDT",)):
    windows = {w: SimpleNamespace(symbols=tuple(SimpleNamespace(symbol=s, included=False,
                direction=Metric.missing("fixture")) for s in symbols)) for w in (1, 5, 15)}
    return SimpleNamespace(point_id="fixture", evaluation_boundary_time_ms=boundary,
        partition="development", movement_evaluation=SimpleNamespace(windows=windows))


class OpenInterestFixtures(unittest.TestCase):
    def test_old_layout_accepts_next_day_midnight_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            old_day = date(2025, 11, 3)
            next_midnight = (datetime(2025, 11, 4, tzinfo=timezone.utc)
                             - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)
            _oi(root, rows=[(next_midnight, "BTCUSDT", "321", "654")], day=old_day)

            evidence = oi.load_binance_usdm_open_interest_evidence(
                root, ("BTCUSDT",), next_midnight + 5 * MINUTE,
                next_midnight + 5 * MINUTE)

            observation = evidence.exact("BTCUSDT", next_midnight)
            self.assertIsNotNone(observation)
            self.assertEqual(observation.available_at_ms, next_midnight + 5 * MINUTE)
            self.assertFalse(evidence.packages[0].malformed_rows)

    def test_june_25_layout_transition_marks_midnight_duplicate_ambiguous(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            transition = (datetime(2026, 6, 25, tzinfo=timezone.utc)
                          - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(milliseconds=1)
            _oi(root, rows=[(transition, "BTCUSDT", "321", "654")],
                day=date(2026, 6, 24))
            _oi(root, rows=[(transition, "BTCUSDT", "322", "655")],
                day=date(2026, 6, 25))

            evidence = oi.load_binance_usdm_open_interest_evidence(
                root, ("BTCUSDT",), transition + 5 * MINUTE,
                transition + 5 * MINUTE)

            self.assertEqual(len(evidence.packages), 2)
            self.assertIsNone(evidence.exact("BTCUSDT", transition))
            self.assertEqual(evidence.reason_at("BTCUSDT", transition),
                             "DUPLICATE_SOURCE_TIMESTAMP")
            self.assertEqual(evidence.row_issues,
                             (("BTCUSDT", transition, "DUPLICATE_SOURCE_TIMESTAMP"),))

    def test_exact_endpoints_causality_and_no_gap_carry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _oi(root)
            evidence = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T + 5 * MINUTE)
            row = oix._symbol_output(evidence, _point(), "BTCUSDT", 15)
            self.assertEqual(row["status"], "READY")
            self.assertEqual(row["current_source_time_ms"], BASE + 25 * MINUTE)
            self.assertEqual(row["prior_source_time_ms"], BASE + 10 * MINUTE)
            with localcontext() as context:
                context.prec = 50
                expected = (Decimal(125) / Decimal(110)).ln()
            self.assertEqual(Decimal(row["delta_oi"]), expected)
            self.assertEqual(evidence.as_of("BTCUSDT", T - 5).source_time_ms, BASE + 20 * MINUTE)
            _oi(root, rows=[(BASE + m * MINUTE, "BTCUSDT", "100", "1000") for m in (10, 15, 20)])
            missing = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T)
            unavailable = oix._symbol_output(missing, _point(), "BTCUSDT", 5)
            self.assertEqual(unavailable["status"], "UNAVAILABLE")
            self.assertIsNone(unavailable["delta_oi"])
            self.assertEqual(unavailable["current_source_time_ms"], BASE + 20 * MINUTE)
            self.assertTrue(missing.packages[0].missing_ranges)

    def test_not_yet_available_and_exact_five_minute_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _oi(root, rows=[(BASE + 25 * MINUTE, "BTCUSDT", "100", "1")])
            evidence = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T - 5000, T)
            self.assertEqual(oix._symbol_output(evidence, _point(T - 5000), "BTCUSDT", 5)["reason"], "OI_NOT_YET_AVAILABLE")
            self.assertIsNotNone(evidence.as_of("BTCUSDT", T))

    def test_duplicates_off_grid_malformed_and_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ts = BASE + 25 * MINUTE
            _oi(root, rows=[(ts, "BTCUSDT", "100", "1"), (ts, "BTCUSDT", "101", "1"),
                (ts + 1, "BTCUSDT", "100", "1"), (ts - 5 * MINUTE, "BTCUSDT", "NaN", "1")])
            evidence = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T)
            self.assertIsNone(evidence.exact("BTCUSDT", ts))
            self.assertEqual(evidence.reason_at("BTCUSDT", ts), "DUPLICATE_SOURCE_TIMESTAMP")
            self.assertEqual(evidence.packages[0].off_grid_timestamps_ms, (ts + 1,))
            self.assertTrue(evidence.packages[0].malformed_rows)
            _oi(root, rows=[], header=("timestamp", "oi"))
            self.assertEqual(oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "SCHEMA_MISMATCH")

    def test_integrity_missing_and_symbol_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(oi, "_acquire_item", side_effect=AssertionError("network forbidden")):
                self.assertEqual(oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "MISSING_PACKAGE")
                path = _oi(root, checksum=False)
                self.assertEqual(oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "MISSING_CHECKSUM")
                _oi(root)
                path.write_bytes(path.read_bytes() + b"changed")
                mismatch = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT",), T, T)
                self.assertEqual(mismatch.packages[0].status, "CHECKSUM_MISMATCH")
                self.assertFalse(mismatch.rows)
                _oi(root)
                _oi(root, "ETHUSDT")
                a = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT", "ETHUSDT"), T, T)
                b = oi.load_binance_usdm_open_interest_evidence(root, ("ETHUSDT", "BTCUSDT"), T, T)
                self.assertNotEqual(a.evidence_sha256, b.evidence_sha256)
                self.assertTrue(all(p.checksum_verified for p in a.packages))

    def test_exact_missing_prior_partial_summary_unique_pairs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _oi(root, rows=[(BASE + m * MINUTE, "BTCUSDT", "100", "1000") for m in (15, 20, 25)])
            evidence = oi.load_binance_usdm_open_interest_evidence(root, ("BTCUSDT", "ETHUSDT"), T, T + 5000)
            point = _point(symbols=evidence.configured_symbols)
            good = oix._symbol_output(evidence, point, "BTCUSDT", 5)
            bad = oix._symbol_output(evidence, point, "ETHUSDT", 5)
            self.assertEqual(oix._symbol_output(evidence, point, "BTCUSDT", 15)["reason"], "MISSING_EXACT_OI_ENDPOINT")
            summary = oix._market_summary((good, bad))
            self.assertEqual(summary["summary_denominator"], 1)
            self.assertTrue(summary["partial_coverage"])
            outputs = tuple(oix.OpenInterestPointOutput(str(i), T + i * 5000, "development",
                (oix._freeze({"window_minutes": 5, "symbols": (good, bad)}),)) for i in (0, 1))
            summaries = oix._partition_summaries(outputs, evidence.configured_symbols)
            self.assertEqual(summaries["all"][0]["ready_symbol_point_count"], 2)
            self.assertEqual(summaries["all"][0]["unique_source_endpoint_pair_count"], 1)


class FundingFixtures(unittest.TestCase):
    def test_settlement_boundary_variable_interval_and_expiry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _funding(root)
            _funding(root, rows=[(BASE - 60 * MINUTE, 1, "0.0001")], month=date(2026, 8, 1))
            evidence = funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, BASE + 2 * 60 * MINUTE)
            self.assertEqual(evidence.as_of("BTCUSDT", BASE).source_time_ms, BASE - 60 * MINUTE)
            current = fx._symbol_output(evidence, _point(BASE + 5000), "BTCUSDT", 5)
            self.assertEqual(current["funding_per_hour"], "-0.0002")
            self.assertEqual(current["funding_interval_hours"], 1)
            self.assertEqual(fx._symbol_output(evidence, _point(BASE + 60 * MINUTE), "BTCUSDT", 5)["status"], "READY")
            expired = fx._symbol_output(evidence, _point(BASE + 60 * MINUTE + 5000), "BTCUSDT", 5)
            self.assertEqual(expired["reason"], "EXPECTED_SETTLEMENT_MISSING")
            self.assertIsNone(expired["funding_per_hour"])
            self.assertEqual(expired["funding_rate"], "-0.0002")
            _funding(root, rows=[(BASE, 4, "0.0008"), (BASE + 4 * 60 * MINUTE, 8, "0")])
            evidence = funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, BASE + 5 * 60 * MINUTE)
            self.assertEqual(fx._symbol_output(evidence, _point(BASE + 5000), "BTCUSDT", 5)["funding_per_hour"], "0.0002")
            self.assertEqual(fx._symbol_output(evidence, _point(BASE + 4 * 60 * MINUTE + 5000), "BTCUSDT", 5)["funding_per_hour"], "0")

    def test_not_yet_available_at_settlement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _funding(root)
            evidence = funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, T)
            self.assertEqual(fx._symbol_output(evidence, _point(BASE), "BTCUSDT", 5)["reason"], "FUNDING_NOT_YET_AVAILABLE")
            self.assertEqual(fx._symbol_output(evidence, _point(BASE + 5000), "BTCUSDT", 5)["status"], "READY")

    def test_failures_duplicates_and_missing_preceding_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(funding, "_acquire_item", side_effect=AssertionError("network forbidden")):
                _funding(root, rows=[(BASE, 8, "0.1"), (BASE, 8, "0.2"), (BASE + MINUTE, 0, "0.1")])
                evidence = funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, T)
                self.assertIsNone(evidence.as_of("BTCUSDT", T))
                self.assertEqual(evidence.packages[0].status, "MISSING_PACKAGE")
                self.assertEqual(evidence.reason_at("BTCUSDT", BASE), "DUPLICATE_SOURCE_TIMESTAMP")
                self.assertTrue(evidence.packages[1].malformed_rows)
                path = _funding(root, checksum=False)
                Path(str(path) + ".CHECKSUM").unlink()
                self.assertEqual(funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, T).packages[1].status, "MISSING_CHECKSUM")
                _funding(root)
                path.write_bytes(path.read_bytes() + b"change")
                self.assertEqual(funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, T).packages[1].status, "CHECKSUM_MISMATCH")
                _funding(root, header=("time", "rate"), rows=[])
                self.assertEqual(funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT",), BASE, T).packages[1].status, "SCHEMA_MISMATCH")

    def test_partial_sign_summary_unique_events_and_no_global_context_dependency(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _funding(root, rows=[(BASE, 3, "0.0001")])
            evidence = funding.load_binance_usdm_funding_evidence(root, ("BTCUSDT", "ETHUSDT"), T, T + 5000)
            point = _point(symbols=evidence.configured_symbols)
            with localcontext() as context:
                context.prec = 8
                good = fx._symbol_output(evidence, point, "BTCUSDT", 5)
            with localcontext() as context:
                context.prec = 50
                self.assertEqual(Decimal(good["funding_per_hour"]), Decimal("0.0001") / 3)
            bad = fx._symbol_output(evidence, point, "ETHUSDT", 5)
            self.assertEqual(fx._market_summary((good, bad))["summary_denominator"], 1)
            outputs = tuple(fx.FundingPointOutput(str(i), T + i * 5000, "development",
                (fx._freeze({"window_minutes": 5, "symbols": (good, bad)}),)) for i in (0, 1))
            self.assertEqual(fx._partition_summaries(outputs, evidence.configured_symbols)["all"][0]["unique_settlement_event_count"], 1)


class LiquidationFixtures(unittest.TestCase):
    def test_microsecond_causality_half_open_windows_and_equal_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _gzip(root, [_snapshot((T - MINUTE) * 1000), _snapshot((T - MINUTE) * 1000),
                _snapshot((T - 1000) * 1000, T * 1000 + 1, side="buy"),
                _snapshot(T * 1000), _snapshot((T - 1000) * 1000, symbol="OTHERUSDT")])
            evidence = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT", "ETHUSDT"), T, T + 5000)
            point = _point(symbols=evidence.configured_symbols)
            first = lx._symbol_output(evidence, point, "BTCUSDT", 1)
            self.assertEqual(first["observed_snapshot_row_count"], 2)
            self.assertEqual(first["observed_long_liquidation_notional"], "400")
            later = lx._symbol_output(evidence, _point(T + 5000), "BTCUSDT", 1)
            self.assertEqual(later["observed_short_liquidation_notional"], "200")
            self.assertEqual(first["observed_short_liquidation_notional"], "0")
            empty = lx._symbol_output(evidence, point, "ETHUSDT", 1)
            self.assertEqual(empty["status"], "NO_OBSERVED_LIQUIDATION")
            self.assertIsNone(empty["observed_liquidation_imbalance"])
            summary = lx._market_summary((first, empty))
            self.assertEqual(summary["breadth_denominator"], 2)
            self.assertEqual(summary["liquidation_breadth"], "0.5")
            self.assertEqual(summary["largest_symbol_share"], "1")
            self.assertEqual(evidence.packages[0].ignored_symbol_row_count, 1)
            self.assertEqual(len(evidence.rows), 4)

    def test_midnight_coverage_and_no_implicit_network_or_paid_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _gzip(root, [_snapshot((BASE + MINUTE) * 1000)])
            with patch.object(liq, "_acquire_free_day", side_effect=AssertionError("network forbidden")):
                evidence = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), BASE, BASE + 15 * MINUTE)
            for minutes in (1, 5, 15):
                self.assertEqual(lx._symbol_output(evidence, _point(BASE + minutes * MINUTE - 5000), "BTCUSDT", minutes)["status"], "UNAVAILABLE")
                self.assertNotEqual(lx._symbol_output(evidence, _point(BASE + minutes * MINUTE), "BTCUSDT", minutes)["status"], "UNAVAILABLE")
            with patch.object(liq, "_acquire_free_day", side_effect=AssertionError("paid access forbidden")):
                missing = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), BASE + liq.DAY_MS + 15 * MINUTE, T + liq.DAY_MS, download=True)
            self.assertEqual(missing.packages[0].status, "FREE_SAMPLE_UNAVAILABLE")
            self.assertIsNone(lx._symbol_output(missing, _point(BASE + liq.DAY_MS + 15 * MINUTE), "BTCUSDT", 15)["observed_total_liquidation_notional"])

    def test_malformed_empty_schema_truncated_and_whole_file_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = _gzip(root, [_snapshot((T - 1000) * 1000, amount="NaN")])
            self.assertEqual(liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "MALFORMED_ROW")
            _gzip(root, [])
            self.assertEqual(liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "EMPTY_PACKAGE")
            _gzip(root, [], header=("symbol", "time"))
            self.assertEqual(liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "SCHEMA_MISMATCH")
            _gzip(root, [_snapshot((T - 1000) * 1000)])
            path.write_bytes(path.read_bytes()[:-8])
            self.assertEqual(liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T).packages[0].status, "INVALID_ARCHIVE")
            _gzip(root, [_snapshot((T - 1000) * 1000, symbol="OTHERUSDT")])
            first = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T)
            _gzip(root, [_snapshot((T - 1000) * 1000, symbol="OTHERUSDT", amount="3")])
            second = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T)
            self.assertFalse(first.rows)
            self.assertNotEqual(first.evidence_sha256, second.evidence_sha256)

    def test_exact_products_sums_and_partial_market(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            price = "123456789012345678901234567890123456789012345678901234567890"
            _gzip(root, [_snapshot((T - 1000) * 1000, price=price, amount="1"),
                         _snapshot((T - 1000) * 1000, side="buy", price="1", amount="0.1")])
            evidence = liq.load_tardis_liquidation_evidence(root, ("BTCUSDT",), T, T)
            with localcontext() as context:
                context.prec = 7
                row = lx._symbol_output(evidence, _point(), "BTCUSDT", 15)
            self.assertEqual(row["observed_total_liquidation_notional"], price + ".1")
            unavailable = dict(row, symbol="ETHUSDT", status="UNAVAILABLE", observed_total_liquidation_notional=None)
            summary = lx._market_summary((row, unavailable))
            self.assertEqual(summary["summary_denominator"], 1)
            self.assertTrue(summary["partial_coverage"])


class PreparedContractsAndIdentities(unittest.TestCase):
    def test_reports_preserve_core_and_fixed_suite_and_reject_changed_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            request = _request(root / "core")
            before = batch.run_historical_experiment_batch(request)
            dataset = load_binance_usdm_historical_replay_dataset(BinanceUSDMArchiveRequest(request.archive_root, request.universe, request.replay_config))
            replay = run_historical_market_replay(dataset.replay_request)
            points = to_market_state_experiment_points(replay, request.partition_plan)
            day = datetime.fromtimestamp(OUTPUT // 1000, tz=timezone.utc).date()
            for symbol in SYMBOLS:
                _oi(root / "oi", symbol, rows=[(OUTPUT - m * MINUTE, symbol, "100", "1000") for m in (5, 10, 15, 20)], day=day)
                _funding(root / "funding", symbol, rows=[(OUTPUT - 60 * MINUTE, 8, "0.0008")], month=day.replace(day=1))
            _gzip(root / "liq", [_snapshot((OUTPUT - 1000) * 1000)], day=day)
            evidence_list = (
                oi.load_binance_usdm_open_interest_evidence(root / "oi", SYMBOLS, OUTPUT, OUTPUT + 15_000),
                funding.load_binance_usdm_funding_evidence(root / "funding", SYMBOLS, OUTPUT, OUTPUT + 15_000),
                liq.load_tardis_liquidation_evidence(root / "liq", SYMBOLS, OUTPUT, OUTPUT + 15_000))
            fingerprints = []
            for module, cls, evidence, source_root, field in (
                (oix, oix.HistoricalOpenInterestExtensionPrepared, evidence_list[0], root / "oi", "oi_evidence"),
                (fx, fx.HistoricalFundingExtensionPrepared, evidence_list[1], root / "funding", "funding_evidence"),
                (lx, lx.HistoricalLiquidationExtensionPrepared, evidence_list[2], root / "liq", "liquidation_evidence")):
                prepared = cls(dataset, replay, points, request.partition_plan, evidence, source_root)
                name = {oix: "open_interest", fx: "funding", lx: "liquidation"}[module]
                runner = getattr(module, "run_historical_" + name + "_extension")
                serialize = getattr(module, "historical_" + name + "_extension_report_to_json")
                report = runner(prepared, code_revision="fixture")
                self.assertEqual(serialize(report), serialize(runner(prepared, code_revision="fixture")))
                self.assertEqual(len(report.candidate_points), len(points))
                self.assertEqual(report.manifest.replay_identity["run_fingerprint"], replay.manifest.run_fingerprint)
                self.assertEqual(report.manifest.replay_identity["experiment_stream_sha256"], before.suite_manifest.experiment_stream_sha256)
                revised = runner(prepared, code_revision="other")
                self.assertEqual(report.manifest.candidate_output_sha256, revised.manifest.candidate_output_sha256)
                self.assertNotEqual(report.manifest.extension_run_fingerprint, revised.manifest.extension_run_fingerprint)
                fingerprints.append(report.manifest.extension_run_fingerprint)
                with self.assertRaises(ValueError):
                    serialize(replace(report, report_sha256="0" * 64))
                with self.assertRaises(ValueError):
                    runner(replace(prepared, experiment_points=points[:-1]), code_revision="fixture")
                with self.assertRaises(ValueError):
                    runner(replace(prepared, experiment_points=(replace(points[0], partition="test"), *points[1:])), code_revision="fixture")
                with self.assertRaises(ValueError):
                    runner(replace(prepared, experiment_points=(replace(points[0], movement_evaluation=replace(points[0].movement_evaluation)), *points[1:])), code_revision="fixture")
                changed_source = replace(evidence, rows=evidence.rows[:-1])
                changed_report = runner(replace(prepared, **{field: changed_source}), code_revision="fixture")
                self.assertNotEqual(report.manifest.extension_run_fingerprint, changed_report.manifest.extension_run_fingerprint)
                reordered = replace(evidence, configured_symbols=tuple(reversed(SYMBOLS)))
                self.assertNotEqual(evidence.evidence_sha256, reordered.evidence_sha256)
                with self.assertRaises(ValueError):
                    runner(replace(prepared, **{field: reordered}), code_revision="fixture")
                with self.assertRaises(ValueError):
                    runner(replace(prepared, **{field: replace(evidence, requested_end_boundary_time_ms=OUTPUT + 20_000)}), code_revision="fixture")
                with self.assertRaises(ValueError):
                    runner(replace(prepared, replay_result=replace(replay, manifest=replace(replay.manifest, dataset_content_sha256="0" * 64))), code_revision="fixture")
            self.assertEqual(len(set(fingerprints)), 3)
            after = batch.run_historical_experiment_batch(request)
            self.assertEqual(batch.historical_experiment_report_to_json(before), batch.historical_experiment_report_to_json(after))
            self.assertEqual(after.experiment_run_count, 28)

    def test_cli_downloads_are_explicit_and_separate(self):
        for module, name, flag in ((oix, "open_interest", "download_oi_archives"),
                                  (fx, "funding", "download_funding_archives"),
                                  (lx, "liquidation", "download_free_liquidation_samples")):
            parser = getattr(module, "build_" + name + "_extension_cli_parser")()
            self.assertFalse(parser.get_default(flag))


if __name__ == "__main__":
    unittest.main()
