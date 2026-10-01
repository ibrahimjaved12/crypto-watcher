"""Focused resolver fixtures; network acquisition and scientific execution are mocked."""

from datetime import date
from functools import lru_cache
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from market_analysis import historical_market_state_study as study
from market_analysis import historical_market_state_study_resolver as resolver
from market_analysis.binance_historical_archive import (
    ARCHIVE_FIRST_SEEN_POLICY, ARCHIVE_SOURCE_STATE_POLICY,
    BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
    BINANCE_ARCHIVE_DATASET_VERSION, BinanceArchiveBundleManifest,
    BinanceArchiveFileIdentity, _content_sha256, daily_aggtrades_relative_path,
    daily_kline_relative_path, historical_candle_start_ms, required_aggtrade_dates,
    required_kline_dates,
)


MINUTE_MS = 60_000


def finalized_record(utc_date: date, *, state="ELIGIBLE"):
    config = study.study_replay_config(utc_date)
    digest = hashlib.sha256(utc_date.isoformat().encode("ascii")).hexdigest()
    files = tuple(sorted((
        BinanceArchiveFileIdentity(
            builder(symbol, package_day).as_posix(), family, symbol, package_day, digest)
        for symbol in study.ORDERED_SYMBOLS
        for family, days, builder in (
            ("aggTrades", required_aggtrade_dates(config), daily_aggtrades_relative_path),
            ("klines/1m", required_kline_dates(config), daily_kline_relative_path))
        for package_day in days), key=lambda item: item.relative_path))
    archive_manifest = BinanceArchiveBundleManifest(
        BINANCE_ARCHIVE_ADAPTER_VERSION, BINANCE_ARCHIVE_DATASET_ID,
        BINANCE_ARCHIVE_DATASET_VERSION, ARCHIVE_FIRST_SEEN_POLICY,
        ARCHIVE_SOURCE_STATE_POLICY, files, _content_sha256(files))
    start = historical_candle_start_ms(config)
    end = config.output_end_boundary_time_ms
    expected_minutes = (end - start) // MINUTE_MS
    coverage = tuple(study.SymbolCoreCandleCoverage(
        symbol, expected_minutes - (1 if state == "INELIGIBLE" and index == 0 else 0),
        (study.KlineGapRange(start, start + MINUTE_MS),)
        if state == "INELIGIBLE" and index == 0 else ())
        for index, symbol in enumerate(study.ORDERED_SYMBOLS))
    provenance = study.CoreDateProvenance(
        config, archive_manifest, digest, start, end, coverage)
    return study.CoreDateEligibility(
        utc_date, state,
        study.CORE_KLINE_GAP if state == "INELIGIBLE" else study.ELIGIBLE_CORE_DATA,
        provenance)


def unverified_record(utc_date: date):
    return study.CoreDateEligibility(
        utc_date, "UNVERIFIED", study.LOCAL_ARCHIVE_MISSING,
        missing_local_paths=("BTCUSDT/aggTrades/example.zip",))


@lru_cache(maxsize=1)
def finalized_manifest():
    records = tuple(finalized_record(
        study._rotated_candidates(calendar_bin, calendar_bin.target_weekday)[0])
        for calendar_bin in study.calendar_bins())
    return study.build_historical_market_state_study_manifest(records)


def unresolved(bin_index=0, weekday=None, utc_date=None):
    calendar_bin = study.calendar_bins()[bin_index]
    day = utc_date or study._rotated_candidates(
        calendar_bin, calendar_bin.target_weekday if weekday is None else weekday)[0]
    return study.UnresolvedStudySelectionError(
        bin_index, day.weekday() if weekday is None else weekday, day)


class ResolverTests(unittest.TestCase):
    def test_local_eligible_candidate_skips_network_and_only_blocks_current_date(self):
        manifest = finalized_manifest()
        candidate = manifest.selection_evidence[0].attempted_dates[0].utc_date
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "new" / "output"
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(utc_date=candidate), manifest]) as build, \
                    patch.object(study, "verify_local_core_date",
                                 return_value=finalized_record(candidate)) as verify, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=AssertionError("network must not be used")), \
                    patch.object(resolver, "_write_report", wraps=resolver._write_report) as write:
                result = resolver.resolve_study("/unused/archive", output_dir, emit=lambda _: None)
            self.assertIs(result, manifest)
            self.assertEqual(build.call_count, 2)
            verify.assert_called_once_with(candidate, "/unused/archive")
            self.assertEqual(write.call_count, 1)
            self.assertEqual(len(result.selected_periods), 30)
            self.assertEqual(tuple(item.phase for item in result.selected_periods),
                             ("development",) * 10 + ("validation",) * 8 + ("test",) * 12)
            self.assertTrue((output_dir / resolver.ELIGIBILITY_REPORT_FILENAME).is_file())
            manifest_path = output_dir / resolver.MANIFEST_FILENAME
            self.assertEqual(manifest_path.read_text(encoding="utf-8"),
                             study.historical_market_state_study_manifest_json(manifest) + "\n")

    def test_local_ineligible_is_checkpointed_before_deterministic_advance(self):
        first_bin = study.calendar_bins()[0]
        candidates = study._rotated_candidates(first_bin, first_bin.target_weekday)
        manifest = finalized_manifest()
        outputs = []
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(utc_date=candidates[0]),
                                           unresolved(utc_date=candidates[1]), manifest]), \
                    patch.object(study, "verify_local_core_date", side_effect=[
                        finalized_record(candidates[0], state="INELIGIBLE"),
                        finalized_record(candidates[1])]) as verify, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=AssertionError("network must not be used")), \
                    patch.object(resolver, "_write_report", wraps=resolver._write_report) as write:
                resolver.resolve_study("/unused/archive", temporary, emit=outputs.append)
            self.assertEqual(verify.call_args_list, [
                call(candidates[0], "/unused/archive"),
                call(candidates[1], "/unused/archive"),
            ])
            self.assertEqual(write.call_count, 2)
            saved = study.parse_historical_study_eligibility_report_json(
                (Path(temporary) / resolver.ELIGIBILITY_REPORT_FILENAME).read_text())
            states = {item.utc_date: item.state for item in saved.eligibility_records}
            self.assertEqual(states[candidates[0]], "INELIGIBLE")
            self.assertEqual(states[candidates[1]], "ELIGIBLE")
            self.assertTrue(any("INELIGIBLE (CORE_KLINE_GAP)" in line for line in outputs))
            self.assertTrue(any("selector advancing deterministically" in line for line in outputs))

    def test_unverified_local_candidate_acquires_once_and_reuses_acquired_dataset(self):
        manifest = finalized_manifest()
        candidate = manifest.selection_evidence[0].attempted_dates[0].utc_date
        dataset = object()
        acquired = type("Acquisition", (), {"dataset": dataset})()
        request = Mock()
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(utc_date=candidate), manifest]), \
                    patch.object(study, "verify_local_core_date",
                                 return_value=unverified_record(candidate)) as verify, \
                    patch.object(resolver, "BinanceHistoricalDownloadRequest",
                                 return_value=request) as request_type, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 return_value=acquired) as acquire, \
                    patch.object(study, "core_date_eligibility_from_verified_dataset",
                                 return_value=finalized_record(candidate)) as classify, \
                    patch.object(study, "load_binance_usdm_historical_replay_dataset",
                                 side_effect=AssertionError("acquired dataset must not be loaded again")) as load:
                resolver.resolve_study("/archive", temporary, emit=lambda _: None)
            verify.assert_called_once_with(candidate, "/archive")
            request_type.assert_called_once_with(
                Path("/archive"), study.study_universe(), study.study_replay_config(candidate))
            acquire.assert_called_once_with(request)
            classify.assert_called_once_with(candidate, dataset)
            load.assert_not_called()
            saved = study.parse_historical_study_eligibility_report_json(
                (Path(temporary) / resolver.ELIGIBILITY_REPORT_FILENAME).read_text())
            self.assertEqual({item.utc_date: item.state for item in saved.eligibility_records}[candidate],
                             "ELIGIBLE")

    def test_acquisition_failure_keeps_blocking_candidate_unverified_and_stops(self):
        manifest = finalized_manifest()
        candidate = manifest.selection_evidence[0].attempted_dates[0].utc_date
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=unresolved(utc_date=candidate)) as build, \
                    patch.object(study, "verify_local_core_date",
                                 return_value=unverified_record(candidate)) as verify, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=OSError("simulated offline")) as acquire:
                with self.assertRaisesRegex(resolver.StudyResolutionError,
                                            "candidate remains UNVERIFIED"):
                    resolver.resolve_study("/archive", temporary, emit=lambda _: None)
            self.assertEqual(build.call_count, 1)
            verify.assert_called_once_with(candidate, "/archive")
            acquire.assert_called_once()
            saved = study.parse_historical_study_eligibility_report_json(
                (Path(temporary) / resolver.ELIGIBILITY_REPORT_FILENAME).read_text())
            self.assertEqual({item.utc_date: item.state for item in saved.eligibility_records}[candidate],
                             "UNVERIFIED")
            self.assertNotIn("/archive", (Path(temporary)
                                           / resolver.ELIGIBILITY_REPORT_FILENAME).read_text())

    def test_no_eligible_date_error_fails_without_inventing_a_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=study.NoEligibleStudyDateError(7)), \
                    patch.object(study, "verify_local_core_date") as verify, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives") as acquire:
                with self.assertRaisesRegex(resolver.StudyResolutionError,
                                            "no eligible core date"):
                    resolver.resolve_study("/archive", temporary, emit=lambda _: None)
            verify.assert_not_called()
            acquire.assert_not_called()

    def test_resume_skips_resolved_prefix_and_matches_uninterrupted_manifest(self):
        first_bin, second_bin = study.calendar_bins()[:2]
        first = study._rotated_candidates(first_bin, first_bin.target_weekday)[0]
        second = study._rotated_candidates(second_bin, second_bin.target_weekday)[0]
        manifest = finalized_manifest()
        with tempfile.TemporaryDirectory() as interrupted_dir, \
                tempfile.TemporaryDirectory() as uninterrupted_dir:
            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(0, utc_date=first),
                                           unresolved(1, utc_date=second)]), \
                    patch.object(study, "verify_local_core_date", side_effect=[
                        finalized_record(first), unverified_record(second)]), \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=OSError("simulated interruption")):
                with self.assertRaises(resolver.StudyResolutionError):
                    resolver.resolve_study("/archive", interrupted_dir, emit=lambda _: None)

            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(1, utc_date=second), manifest]), \
                    patch.object(study, "verify_local_core_date",
                                 return_value=finalized_record(second)) as verify, \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=AssertionError("saved eligible prefix must not reacquire")):
                resolver.resolve_study("/archive", interrupted_dir, emit=lambda _: None)
            verify.assert_called_once_with(second, "/archive")

            with patch.object(study, "build_historical_market_state_study_manifest",
                              side_effect=[unresolved(0, utc_date=first),
                                           unresolved(1, utc_date=second), manifest]), \
                    patch.object(study, "verify_local_core_date", side_effect=[
                        finalized_record(first), finalized_record(second)]), \
                    patch.object(resolver, "acquire_binance_usdm_historical_archives",
                                 side_effect=AssertionError("local candidates must not download")):
                resolver.resolve_study("/archive", uninterrupted_dir, emit=lambda _: None)
            self.assertEqual(
                (Path(interrupted_dir) / resolver.MANIFEST_FILENAME).read_bytes(),
                (Path(uninterrupted_dir) / resolver.MANIFEST_FILENAME).read_bytes())

    def test_existing_identical_manifest_is_accepted_but_different_is_preserved(self):
        manifest = finalized_manifest()
        content = study.historical_market_state_study_manifest_json(manifest)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / resolver.MANIFEST_FILENAME
            path.write_bytes((content + "\n").encode("utf-8"))
            with patch.object(study, "build_historical_market_state_study_manifest",
                              return_value=manifest):
                resolver.resolve_study("/archive", root, emit=lambda _: None)
            self.assertEqual(path.read_bytes(), (content + "\n").encode("utf-8"))

            path.write_bytes(b"different frozen manifest\n")
            with patch.object(study, "build_historical_market_state_study_manifest",
                              return_value=manifest):
                with self.assertRaisesRegex(resolver.StudyResolutionError,
                                            "refusing to overwrite"):
                    resolver.resolve_study("/archive", root, emit=lambda _: None)
            self.assertEqual(path.read_bytes(), b"different frozen manifest\n")

    def test_malformed_saved_progress_is_not_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            progress = Path(temporary) / resolver.ELIGIBILITY_REPORT_FILENAME
            progress.write_text("{malformed", encoding="utf-8")
            with patch.object(study, "build_historical_market_state_study_manifest") as build:
                with self.assertRaises(ValueError):
                    resolver.resolve_study("/archive", temporary, emit=lambda _: None)
            build.assert_not_called()
            self.assertEqual(progress.read_text(encoding="utf-8"), "{malformed")

    def test_no_replay_or_experiment_entrypoint_is_used(self):
        manifest = finalized_manifest()
        with tempfile.TemporaryDirectory() as temporary, \
                patch("market_analysis.historical_replay.run_historical_market_replay",
                      side_effect=AssertionError("replay forbidden")) as replay, \
                patch("market_analysis.historical_experiment_batch.run_historical_experiment_batch",
                      side_effect=AssertionError("experiments forbidden")) as batch, \
                patch.object(study, "build_historical_market_state_study_manifest",
                             return_value=manifest):
            resolver.resolve_study("/archive", temporary, emit=lambda _: None)
            replay.assert_not_called()
            batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
