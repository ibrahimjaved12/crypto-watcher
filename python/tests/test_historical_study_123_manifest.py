"""Focused contract fixtures; no real archives, replay or candidate execution."""

import csv
from dataclasses import FrozenInstanceError, replace
from datetime import date, timedelta
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from market_analysis import historical_study_123_manifest as study
from market_analysis.binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, ARCHIVE_SOURCE_STATE_POLICY,
    BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
    BINANCE_ARCHIVE_DATASET_VERSION, BinanceArchiveBundleManifest,
    BinanceArchiveFileIdentity, _content_sha256,
    daily_aggtrades_relative_path, daily_kline_relative_path,
    historical_candle_start_ms, required_aggtrade_dates, required_kline_dates,
)
from market_analysis.historical_experiment_batch import EXPERIMENT_SUITE_V1, _sha256
from market_analysis.movement_metrics import MarketMovementConfig, MarketUniverseInput


MINUTE = 60_000
DAY = 86_400_000
AGG_HEADER = ["agg_trade_id", "price", "quantity", "first_trade_id",
              "last_trade_id", "transact_time", "is_buyer_maker"]
KLINE_HEADER = ["open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "number_of_trades",
                "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"]


def synthetic_eligible(day):
    """Finalized synthetic provenance for selection-only tests, never loaded."""
    config = study.study_replay_config(day)
    start, end = historical_candle_start_ms(config), config.output_end_boundary_time_ms
    digest = hashlib.sha256(day.isoformat().encode("ascii")).hexdigest()
    files = tuple(sorted((
        BinanceArchiveFileIdentity(builder(symbol, package_day).as_posix(), family,
                                   symbol, package_day, digest)
        for symbol in study.ORDERED_SYMBOLS
        for family, days, builder in (
            ("aggTrades", required_aggtrade_dates(config), daily_aggtrades_relative_path),
            ("klines/1m", required_kline_dates(config), daily_kline_relative_path))
        for package_day in days), key=lambda item: item.relative_path))
    archive = BinanceArchiveBundleManifest(
        BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
        BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY,
        ARCHIVE_SOURCE_STATE_POLICY, files, _content_sha256(files))
    provenance = study.CoreDateProvenance(
        config, archive, digest, start, end,
        tuple(study.SymbolCoreCandleCoverage(symbol, (end - start) // MINUTE)
              for symbol in study.ORDERED_SYMBOLS))
    return study.CoreDateEligibility(day, "ELIGIBLE", study.ELIGIBLE_CORE_DATA, provenance)


def ineligible(record):
    provenance = record.provenance
    first = provenance.symbol_coverage[0]
    gap = study.KlineGapRange(provenance.candle_start_open_time_ms,
                             provenance.candle_start_open_time_ms + MINUTE)
    changed = replace(provenance, symbol_coverage=(
        replace(first, observed_minute_count=first.observed_minute_count - 1, gaps=(gap,)),
        *provenance.symbol_coverage[1:]))
    return study.CoreDateEligibility(record.utc_date, "INELIGIBLE", study.CORE_KLINE_GAP, changed)


@lru_cache(maxsize=1)
def synthetic_calendar():
    return tuple(synthetic_eligible(day) for item in study.calendar_bins() for day in item.dates)


def write_archive(root, relative, rows, header):
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(destination, "w") as archive:
        member = zipfile.ZipInfo(f"{relative.stem}.csv", date_time=(1980, 1, 1, 0, 0, 0))
        member.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(member, text.getvalue())
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    Path(f"{destination}.CHECKSUM").write_text(f"{digest}  {relative.name}\n", encoding="utf-8")


def kline_rows(config, package_day, missing=()):
    midnight = (package_day - date(1970, 1, 1)).days * DAY
    start = max(midnight, historical_candle_start_ms(config))
    end = min(midnight + DAY, config.output_end_boundary_time_ms)
    return ([str(opening), "100", "102", "99", "101", "2",
             str(opening + MINUTE - 1), "202", "3", "1", "100", "0"]
            for opening in range(start, end, MINUTE) if opening not in missing)


class StudySelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bins = study.calendar_bins()
        cls.records = synthetic_calendar()
        cls.manifest = study.build_study123_manifest(cls.records)

    def test_exact_calendar_partition(self):
        self.assertEqual((study.CALENDAR_END - study.CALENDAR_START).days + 1, 974)
        self.assertEqual(len(self.bins), 30)
        days = tuple(day for item in self.bins for day in item.dates)
        self.assertEqual(len(days), len(set(days)))
        self.assertEqual(days, tuple(study.CALENDAR_START + timedelta(days=i) for i in range(974)))
        self.assertEqual({len(item.dates) for item in self.bins}, {32, 33})
        for left, right in zip(self.bins, self.bins[1:]):
            self.assertEqual(left.end_date + timedelta(days=1), right.start_date)
        with self.assertRaises(ValueError):
            replace(self.bins[0], end_date=self.bins[0].end_date + timedelta(days=1))
        with patch.object(study, "CALENDAR_END", date(2023, 1, 1)):
            with self.assertRaises(ValueError):
                study.calendar_bins()

    def test_fixed_weekday_hash_examples_and_order_invariance(self):
        expected = {0: date(2024, 1, 22), 1: date(2024, 2, 13), 29: date(2026, 8, 4)}
        periods = self.manifest.selected_periods
        self.assertEqual(periods, study.select_study_periods(reversed(self.records)))
        for period in periods:
            self.assertEqual(period.target_weekday, period.source_bin_index % 7)
            self.assertEqual(period.selected_weekday, period.target_weekday)
            self.assertFalse(period.weekday_fallback_needed)
            self.assertEqual(period.fallback_distance, 0)
            self.assertLessEqual(period.bin_start_date, period.utc_date)
            self.assertLessEqual(period.utc_date, period.bin_end_date)
        for index, day in expected.items():
            self.assertEqual(periods[index].utc_date, day)

    def test_cyclic_fallback_same_digest_and_no_eligible_dates(self):
        first = self.bins[0]
        records = tuple(ineligible(item) if item.utc_date in first.dates
                        and item.utc_date.weekday() == first.target_weekday else item
                        for item in self.records)
        result = study.select_study_date(first, records)
        self.assertEqual(result.fallback_distance, 1)
        self.assertEqual(result.core_eligibility.utc_date.weekday(), 1)
        candidates = tuple(day for day in first.dates if day.weekday() == 1)
        seed = int.from_bytes(hashlib.sha256(b"crypto-watcher:study-123-v1:0").digest(), "big")
        self.assertEqual(result.core_eligibility.utc_date, candidates[seed % len(candidates)])
        wrapped_bin = self.bins[6]  # Sunday -> Monday.
        wrapped_records = tuple(ineligible(item) if item.utc_date in wrapped_bin.dates
                                and item.utc_date.weekday() == 6 else item for item in self.records)
        wrapped = study.select_study_date(wrapped_bin, wrapped_records)
        self.assertEqual(wrapped.fallback_distance, 1)
        self.assertEqual(wrapped.core_eligibility.utc_date.weekday(), 0)
        none = tuple(ineligible(item) if item.utc_date in first.dates else item for item in self.records)
        with self.assertRaises(study.NoEligibleStudyDateError) as caught:
            study.select_study_date(first, none)
        self.assertEqual(caught.exception.bin_index, 0)

    def test_unverified_target_and_fallback_dates_block_final_selection(self):
        first = self.bins[0]
        unknown = next(day for day in first.dates if day.weekday() == 0)
        records = tuple(study.CoreDateEligibility(item.utc_date, "UNVERIFIED", study.LOCAL_ARCHIVE_MISSING)
                        if item.utc_date == unknown else item for item in self.records)
        with self.assertRaises(study.UnresolvedStudySelectionError) as caught:
            study.select_study_date(first, records)
        self.assertEqual(caught.exception.unresolved_dates, (unknown,))
        self.assertEqual(caught.exception.weekday, 0)
        with self.assertRaises(study.UnresolvedStudySelectionError):
            study.select_study_date(first, ())  # absent eligibility is unresolved, never skipped.
        tuesday = next(day for day in first.dates if day.weekday() == 1)
        fallback = tuple(ineligible(item) if item.utc_date in first.dates and item.utc_date.weekday() == 0
                         else study.CoreDateEligibility(item.utc_date, "UNVERIFIED",
                                                       study.LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION)
                         if item.utc_date == tuesday else item for item in self.records)
        with self.assertRaises(study.UnresolvedStudySelectionError) as caught:
            study.select_study_date(first, fallback)
        self.assertEqual(caught.exception.weekday, 1)
        self.assertEqual(caught.exception.unresolved_dates, (tuesday,))

    def test_unconsidered_weekday_can_remain_unverified(self):
        first = self.bins[0]
        records = tuple(study.CoreDateEligibility(item.utc_date, "UNVERIFIED", study.LOCAL_ARCHIVE_MISSING)
                        if item.utc_date in first.dates and item.utc_date.weekday() != 0
                        else item for item in self.records)
        self.assertEqual(study.select_study_date(first, records).core_eligibility.utc_date,
                         date(2024, 1, 22))

    def test_study_phases_and_exact_midnight_boundaries(self):
        periods = self.manifest.selected_periods
        self.assertEqual(tuple(item.phase for item in periods),
                         ("development",) * 10 + ("validation",) * 8 + ("test",) * 12)
        self.assertEqual(tuple(item.study_period_index for item in periods), tuple(range(30)))
        self.assertEqual(tuple(item.utc_date for item in periods), tuple(sorted(item.utc_date for item in periods)))
        for item in periods:
            self.assertEqual(item.start_boundary_time_ms % DAY, 0)
            self.assertEqual(item.end_boundary_time_ms - item.start_boundary_time_ms, DAY)
        self.assertFalse(hasattr(study, "ReplayPartitionPlan"))
        selections = tuple(study.select_study_date(item, self.records) for item in self.bins)
        self.assertEqual(study.assign_study_phases(reversed(selections)), periods)
        for invalid in (selections[:-1], (selections[0],) * 30):
            with self.assertRaises(ValueError):
                study.assign_study_phases(invalid)
        with self.assertRaises(ValueError):
            replace(periods[0], phase="test")
        with self.assertRaises(ValueError):
            replace(periods[0], utc_date=study.CALENDAR_END)

    def test_manifest_and_report_identity_are_canonical_and_immutable(self):
        manifest = self.manifest
        reordered = study.build_study123_manifest(reversed(self.records))
        self.assertEqual(study.study123_manifest_json(manifest), study.study123_manifest_json(reordered))
        payload = json.loads(study.study123_manifest_json(manifest))
        digest = payload.pop("manifest_sha256")
        self.assertEqual(digest, _sha256(payload))
        self.assertNotEqual(digest, _sha256({**payload, "manifest_sha256": digest}))
        self.assertEqual(payload["calendar_start"], "2024-01-01")
        self.assertEqual(payload["secondary_horizons_minutes"], [1, 5, 15, 30, 60])
        text = study.study123_manifest_json(manifest)
        for forbidden in ("archive_root", "code_revision", "generated_at", "wall_time", "machine_name"):
            self.assertNotIn(forbidden, text)
        a = study.Study123EligibilityReport(self.records)
        b = study.Study123EligibilityReport(tuple(reversed(self.records)))
        self.assertEqual(study.study123_eligibility_report_json(a), study.study123_eligibility_report_json(b))
        report = json.loads(study.study123_eligibility_report_json(a))
        self.assertEqual(report.pop("report_sha256"), _sha256(report))
        with self.assertRaises(FrozenInstanceError):
            manifest.study_version = "changed"
        with self.assertRaises(FrozenInstanceError):
            self.records[0].state = "INELIGIBLE"
        with self.assertRaises(ValueError):
            study.build_study123_manifest((*self.records, self.records[0]))

    def test_scientific_changes_change_hash_and_frozen_design_rejects_overrides(self):
        changed_records = tuple(ineligible(item) if item.utc_date == date(2024, 1, 22)
                                else item for item in self.records)
        changed = study.build_study123_manifest(changed_records)
        self.assertNotEqual(changed.selected_periods[0].utc_date, self.manifest.selected_periods[0].utc_date)
        self.assertNotEqual(changed.manifest_sha256, self.manifest.manifest_sha256)
        payload = json.loads(study.study123_manifest_json(self.manifest))
        payload.pop("manifest_sha256")
        for field_name in ("universe", "primary_hypotheses", "selected_periods"):
            altered = json.loads(json.dumps(payload))
            if field_name == "universe":
                altered[field_name]["symbols"].reverse()
            elif field_name == "primary_hypotheses":
                altered[field_name][0]["research_question"] = "changed"
            else:
                altered[field_name][0]["utc_date"] = "2024-01-29"
            self.assertNotEqual(_sha256(payload), _sha256(altered))
        overrides = (
            {"study_version": "study-123-v2"}, {"calendar_end": date(2026, 9, 1)},
            {"universe": MarketUniverseInput(study.UNIVERSE_ID, "v1", study.ORDERED_SYMBOLS[::-1])},
            {"development_period_count": 11}, {"bin_count": 29},
            {"selected_periods": self.manifest.selected_periods[::-1]},
            {"selected_periods": self.manifest.selected_periods[:-1]},
            {"primary_hypotheses": (replace(study.PRIMARY_HYPOTHESES[0], research_question="changed"),
                                    *study.PRIMARY_HYPOTHESES[1:])},
            {"primary_hypotheses": (study.PRIMARY_HYPOTHESES[0],) * 16},
        )
        for override in overrides:
            with self.subTest(override=tuple(override)):
                with self.assertRaises(ValueError):
                    replace(self.manifest, **override)
        before = _sha256(EXPERIMENT_SUITE_V1)
        study.build_study123_manifest(self.records)
        self.assertEqual(_sha256(EXPERIMENT_SUITE_V1), before)
        self.assertEqual(len(EXPERIMENT_SUITE_V1), 28)

    def test_exact_frozen_primary_registry(self):
        expected = (
            ("EXP-75-01", True, 15, "state_persistence"),
            ("EXP-75-02", True, 15, "absolute_market_return"),
            ("EXP-75-03", True, 15, "signed_market_return"),
            ("EXP-75-04A", False, None, "retrospective_structural_reference"),
            ("EXP-75-04B", True, 60, "realized_volatility"),
            ("EXP-75-05", True, 5, "persistence_weakening"),
            ("EXP-75-06A", True, 15, "absolute_market_return"),
            ("EXP-75-06B", True, 15, "absolute_market_return"),
            ("EXP-75-07", True, 15, "future_breadth_extremity"),
            ("EXP-75-08", True, 15, "forward_dispersion"),
            ("EXP-75-09", True, 60, "realized_volatility"),
            ("EXP-75-10", True, 5, "signed_market_return"),
            ("EXP-75-11-OI", True, 15, "v1_direction_persistence"),
            ("EXP-75-11-FUNDING", True, 60, "v1_direction_persistence"),
            ("EXP-75-11-LIQUIDATION", True, 15, "realized_volatility"),
            ("EXP-75-12", True, 5, "signed_market_return"),
        )
        self.assertEqual(tuple((item.family_id, item.causal_forward_test,
                                item.primary_horizon_minutes, item.primary_outcome_id)
                               for item in study.PRIMARY_HYPOTHESES), expected)
        self.assertTrue(all(item.display_name and item.research_question for item in study.PRIMARY_HYPOTHESES))
        pelt = study.PRIMARY_HYPOTHESES[3]
        for values in ({"causal_forward_test": True}, {"primary_horizon_minutes": 15}):
            with self.assertRaises(ValueError):
                replace(pelt, **values)
        with self.assertRaises(ValueError):
            replace(study.PRIMARY_HYPOTHESES[0], primary_horizon_minutes=None)

    def test_eligibility_states_and_coverage_provenance_must_agree(self):
        record = self.records[0]
        for values in ({"state": "INELIGIBLE", "reason_code": study.CORE_KLINE_GAP},
                       {"state": "UNVERIFIED", "reason_code": study.LOCAL_ARCHIVE_MISSING},
                       {"provenance": None}):
            with self.assertRaises(ValueError):
                replace(record, **values)
        with self.assertRaises(ValueError):
            study.CoreDateEligibility(record.utc_date, "UNVERIFIED", study.CORE_KLINE_GAP)
        with self.assertRaises(ValueError):
            study.CoreDateEligibility(record.utc_date, "UNVERIFIED", study.LOCAL_ARCHIVE_MISSING,
                                      missing_local_paths=("/absolute/local/archive.zip",))
        with self.assertRaises(ValueError):
            replace(record.provenance, archive_manifest=replace(
                record.provenance.archive_manifest, archive_files=()))
        with self.assertRaises(ValueError):
            replace(record.provenance, symbol_coverage=record.provenance.symbol_coverage[::-1])
        payload = json.loads(study.study123_eligibility_report_json(
            study.Study123EligibilityReport((record,))))["eligibility_records"][0]
        digest = payload.pop("eligibility_sha256")
        self.assertEqual(digest, _sha256(payload))
        provenance = payload["provenance"]
        digest = provenance.pop("provenance_sha256")
        self.assertEqual(digest, _sha256(provenance))


class LocalCoreEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.day = date(2024, 1, 22)
        cls.config = study.study_replay_config(cls.day)
        # Default V1 genuinely needs seven days plus its preceding endpoint.
        # The generated fixture contains only that range (~58k compact rows).
        # Every required aggTrade package is verified but intentionally empty.
        for symbol in study.ORDERED_SYMBOLS:
            for package_day in required_aggtrade_dates(cls.config):
                write_archive(cls.root, daily_aggtrades_relative_path(symbol, package_day), (), AGG_HEADER)
            for package_day in required_kline_dates(cls.config):
                write_archive(cls.root, daily_kline_relative_path(symbol, package_day),
                              kline_rows(cls.config, package_day), KLINE_HEADER)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def copy_fixture(self, root):
        shutil.copytree(self.root, root, dirs_exist_ok=True)

    def test_valid_canonical_archive_no_network_replay_or_trade_gap_rule(self):
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("download forbidden")), \
                patch("market_analysis.historical_replay.run_historical_market_replay",
                      side_effect=AssertionError("replay forbidden")):
            record = study.verify_local_core_date(self.day, self.root)
        self.assertEqual((record.state, record.reason_code), ("ELIGIBLE", study.ELIGIBLE_CORE_DATA))
        self.assertEqual(record.provenance.replay_config.movement_config, MarketMovementConfig())
        expected = (self.config.output_end_boundary_time_ms - historical_candle_start_ms(self.config)) // MINUTE
        self.assertEqual(tuple(item.observed_minute_count for item in record.provenance.symbol_coverage),
                         (expected,) * 5)
        self.assertTrue(all(not item.gaps for item in record.provenance.symbol_coverage))
        self.assertEqual(record.provenance.candle_end_open_time_ms_exclusive,
                         self.config.output_end_boundary_time_ms)

    def test_internal_and_both_edge_gaps_are_scientifically_ineligible(self):
        start, end = historical_candle_start_ms(self.config), self.config.output_end_boundary_time_ms
        for opening in (start, start + MINUTE, end - MINUTE):
            with self.subTest(opening=opening), tempfile.TemporaryDirectory() as local:
                root = Path(local)
                self.copy_fixture(root)
                package_day = date(1970, 1, 1) + timedelta(days=opening // DAY)
                write_archive(root, daily_kline_relative_path("BTCUSDT", package_day),
                              kline_rows(self.config, package_day, (opening,)), KLINE_HEADER)
                record = study.verify_local_core_date(self.day, root)
                self.assertEqual((record.state, record.reason_code), ("INELIGIBLE", study.CORE_KLINE_GAP))
                self.assertEqual(record.provenance.symbol_coverage[0].gaps,
                                 (study.KlineGapRange(opening, opening + MINUTE),))
                self.assertFalse(any(item.gaps for item in record.provenance.symbol_coverage[1:]))

    def test_missing_archive_or_checksum_is_unverified(self):
        relative = daily_aggtrades_relative_path("BTCUSDT", required_aggtrade_dates(self.config)[0])
        for suffix in ("", ".CHECKSUM"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as local:
                root = Path(local)
                self.copy_fixture(root)
                Path(f"{root.joinpath(*relative.parts)}{suffix}").unlink()
                record = study.verify_local_core_date(self.day, root)
                self.assertEqual((record.state, record.reason_code), ("UNVERIFIED", study.LOCAL_ARCHIVE_MISSING))
                self.assertIn(f"{relative}{suffix}", record.missing_local_paths)
                self.assertIsNone(record.provenance)
                self.assertNotIn(local, study.study123_eligibility_report_json(study.Study123EligibilityReport((record,))))

    def test_local_checksum_zip_or_schema_corruption_remains_unverified(self):
        relative = daily_aggtrades_relative_path("BTCUSDT", required_aggtrade_dates(self.config)[0])
        for failure in ("checksum", "zip", "schema"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as local:
                root = Path(local)
                self.copy_fixture(root)
                archive = root.joinpath(*relative.parts)
                if failure == "checksum":
                    Path(f"{archive}.CHECKSUM").write_text(f"{'0' * 64}  {relative.name}\n")
                elif failure == "zip":
                    archive.write_bytes(b"not a ZIP")
                    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
                    Path(f"{archive}.CHECKSUM").write_text(f"{digest}  {relative.name}\n")
                else:
                    write_archive(root, relative, (("bad", "row"),), AGG_HEADER)
                record = study.verify_local_core_date(self.day, root)
                self.assertEqual((record.state, record.reason_code),
                                 ("UNVERIFIED", study.LOCAL_ARCHIVE_INVALID_OR_NEEDS_REACQUISITION))
                self.assertIsNone(record.provenance)
                self.assertNotIn(local, str(record))

    def test_local_root_independence_of_eligibility_and_manifest(self):
        first = study.verify_local_core_date(self.day, self.root)
        with tempfile.TemporaryDirectory() as local:
            root = Path(local)
            self.copy_fixture(root)
            second = study.verify_local_core_date(self.day, root)
        self.assertEqual(first, second)
        self.assertEqual(first.eligibility_sha256, second.eligibility_sha256)
        records = tuple(first if item.utc_date == self.day else item for item in synthetic_calendar())
        reordered = tuple(second if item.utc_date == self.day else item for item in reversed(synthetic_calendar()))
        self.assertEqual(study.build_study123_manifest(records).manifest_sha256,
                         study.build_study123_manifest(reordered).manifest_sha256)
