"""Generated governance fixtures for #123 development HMM cross-fit artifacts."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from market_analysis.experiments.market_state_hmm_regimes import (
    HMM_CONFIG_V1,
    HMMDevelopmentTrainingBlock,
    HMMFeatureRow,
    HMMTrainingDiagnostics,
    _training_fingerprint,
)
from market_analysis.historical_market_state_candidate_evidence import CANDIDATE_EVIDENCE_VERSION
from market_analysis.historical_experiment_batch import report_json_safe
from market_analysis.historical_market_state_study import (
    parse_historical_market_state_study_manifest_json,
)
import market_analysis.historical_market_state_hmm_crossfit as crossfit
import market_analysis.historical_market_state_study_execution as execution


MANIFEST_PATH = (Path(__file__).parents[2] / "research" /
                 "historical-market-state-study-v1" / "selection" /
                 "historical-market-state-study-v1-manifest.json")


@dataclass(frozen=True)
class FixtureModel:
    model_sha256: str
    training_data_sha256: str
    training_block_count: int
    training_usable_row_count: int
    training_unavailable_row_count: int
    training_transition_count: int
    training_first_usable_boundary_time_ms: int
    training_last_usable_boundary_time_ms: int


class HistoricalMarketStateHMMCrossFitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = parse_historical_market_state_study_manifest_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))

    def _write_development_reports(self, root, revision="crossfit-fixture"):
        period_dir = root / execution.PERIOD_DIRECTORY
        period_dir.mkdir(parents=True)
        coverage_sha = "a" * 64
        blocks = []
        for period in self.manifest.selected_periods[:10]:
            row = HMMFeatureRow(
                period.start_boundary_time_ms,
                (float(period.study_period_index + 1), 0.1, 0.2, 1.0))
            block = HMMDevelopmentTrainingBlock(
                period.study_period_index, period.utc_date.isoformat(),
                period.start_boundary_time_ms, period.end_boundary_time_ms,
                ("movement-v1", "config-v1", "universe-v1", "1",
                 ("BTCUSDT", "ETHUSDT"), "provider", "exchange", "trade"),
                ((row,),), 1439)
            blocks.append(block)
            candidate_records = [report_json_safe(execution.HistoricalStudyCandidateEvidence(
                period.study_period_index, period.utc_date.isoformat(), period.phase,
                "V1", "v1-algorithm-fixture", "v1-config-fixture",
                row.evaluation_boundary_time_ms, "CONTINUOUS", "READY", {}))]
            replay_identity = {"movement_algorithm_version": "movement-v1",
                               "movement_config_version": "config-v1", "universe_id": "universe-v1",
                               "universe_version": "1", "configured_universe": ["BTCUSDT", "ETHUSDT"],
                               "provider": "provider", "exchange": "exchange", "price_type": "trade"}
            context = []
            body = {
                "period_report_schema_version": execution.PERIOD_REPORT_SCHEMA_VERSION,
                "execution_version": execution.EXECUTION_VERSION,
                "study_version": self.manifest.study_version,
                "candidate_evidence_version": CANDIDATE_EVIDENCE_VERSION,
                "forward_outcomes_version": execution.FORWARD_OUTCOMES_VERSION,
                "tool_config_version": execution.TOOL_CONFIG_VERSION,
                "study_manifest_sha256": self.manifest.manifest_sha256,
                "extension_coverage_manifest_sha256": coverage_sha,
                "code_revision": revision,
                "period": report_json_safe(period),
                "canonical_replay_manifest": replay_identity,
                "candidate_evidence": candidate_records,
                "candidate_evidence_sha256": execution._digest(candidate_records),
                "v1_evidence_sha256": execution._digest(candidate_records),
                "event_time_v1_context_version": execution.EVENT_TIME_V1_CONTEXT_VERSION,
                "event_time_v1_context": context,
                "event_time_v1_context_sha256": execution._digest({
                    "version": execution.EVENT_TIME_V1_CONTEXT_VERSION, "records": context}),
                "bocpd_onset_evidence_version": execution.BOCPD_ONSET_EVIDENCE_VERSION,
                "bocpd_onset_evidence": [],
                "bocpd_onset_evidence_sha256": execution._digest({
                    "version": execution.BOCPD_ONSET_EVIDENCE_VERSION, "records": []}),
                "hmm_development_training_block": report_json_safe(block),
                "hmm_development_training_block_sha256": block.block_sha256,
            }
            payload = execution._artifact_json(body, "report_sha256")
            (period_dir / execution._period_filename(period)).write_text(
                payload, encoding="utf-8")
        return tuple(blocks), coverage_sha

    def test_crossfit_uses_nine_blocks_and_writes_ten_fold_artifacts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocks, coverage_sha = self._write_development_reports(root)
            trained_inputs = []

            def train(training_blocks, config):
                training_blocks = tuple(training_blocks)
                trained_inputs.append(tuple(
                    item.study_period_index for item in training_blocks))
                held_out = next(index for index in range(10)
                                if index not in trained_inputs[-1])
                training_blocks = tuple(block for block in blocks if block.study_period_index != held_out)
                feature_blocks = tuple(feature_block for block in training_blocks
                                       for feature_block in block.feature_blocks)
                training_sha = _training_fingerprint(
                    feature_blocks, training_blocks[0].movement_scope, HMM_CONFIG_V1)
                diagnostics = HMMTrainingDiagnostics(
                    "HMM_TRAINING_READY", None, len(feature_blocks), 9 * 1439,
                    len(feature_blocks), 0, training_blocks[0].start_boundary_time_ms,
                    training_blocks[-1].start_boundary_time_ms, training_sha)
                return diagnostics, FixtureModel(
                    (f"{held_out:02d}" * 32)[:64], training_sha, len(feature_blocks),
                    len(feature_blocks), 9 * 1439, 0,
                    training_blocks[0].start_boundary_time_ms,
                    training_blocks[-1].start_boundary_time_ms)

            def filter_blocks(feature_blocks, model, config):
                return tuple(tuple({
                    "evaluation_boundary_time_ms": row.evaluation_boundary_time_ms,
                    "status": "HMM_READY",
                    "status_reason": None,
                    "raw_feature_vector": list(row.values),
                    "standardized_feature_vector": [0.0, 0.0, 0.0, 0.0],
                    "filter_reset_before_observation": True,
                    "predicted_state_probabilities": [1 / 3, 1 / 3, 1 / 3],
                    "posterior_probabilities": [0.2, 0.6, 0.2],
                    "hard_state": "MID_MOVEMENT",
                    "posterior_confidence": 0.6,
                    "posterior_entropy": 0.0,
                    "predictive_log_likelihood": 0.0,
                    "algorithm_version": crossfit.HMM_ALGORITHM_VERSION,
                    "config_version": config.version,
                    "model_sha256": model.model_sha256,
                } for row in block) for block in feature_blocks)

            def parse_fixture_model(item):
                return FixtureModel(
                    item["model_sha256"], item["training_data_sha256"],
                    item["training_block_count"], item["training_usable_row_count"],
                    item["training_unavailable_row_count"],
                    item["training_transition_count"],
                    item["training_first_usable_boundary_time_ms"],
                    item["training_last_usable_boundary_time_ms"])

            with patch.object(crossfit, "_parse_hmm_model",
                              side_effect=parse_fixture_model), patch.object(
                    crossfit, "train_hmm_regime_model_from_blocks",
                              side_effect=train) as trainer, patch.object(
                    crossfit, "filter_hmm_regime_feature_blocks",
                    side_effect=filter_blocks) as filtering:
                paths = crossfit.freeze_development_hmm_crossfit(
                    self.manifest, root, code_revision="crossfit-fixture")

            self.assertEqual(len(paths), 10)
            self.assertEqual(trainer.call_count, 10)
            self.assertEqual(filtering.call_count, 10)
            self.assertTrue(all(len(indices) == 9 for indices in trained_inputs))
            for held_out, indices in enumerate(trained_inputs):
                self.assertNotIn(held_out, indices)
                self.assertEqual(indices,
                                 tuple(index for index in range(10)
                                       if index != held_out))

            first = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(first["artifact_version"],
                             crossfit.HMM_DEVELOPMENT_CROSSFIT_VERSION)
            self.assertEqual(first["extension_coverage_manifest_sha256"],
                             coverage_sha)
            self.assertEqual(first["held_out_period"]["study_period_index"], 0)
            self.assertEqual(len(first["ordered_training_blocks"]), 9)
            self.assertEqual(
                first["artifact_sha256"],
                execution._verify_hashed_payload(
                    first, "artifact_sha256", "HMM cross-fit fold"))

            index = json.loads(
                (root / crossfit.HMM_CROSSFIT_INDEX_FILENAME).read_text(
                    encoding="utf-8"))
            self.assertEqual(len(index["ordered_folds"]), 10)
            self.assertEqual(
                index["index_sha256"],
                execution._verify_hashed_payload(
                    index, "index_sha256", "HMM cross-fit index"))
            with patch.object(crossfit, "_parse_hmm_model",
                              side_effect=parse_fixture_model):
                validated = crossfit.validate_hmm_crossfit_index(
                    root / crossfit.HMM_CROSSFIT_INDEX_FILENAME, self.manifest,
                    coverage_sha256=coverage_sha, code_revision="crossfit-fixture")
            self.assertEqual(len(validated["ordered_folds"]), 10)
            self.assertEqual(first["fold_model"]["training_block_count"], 9)
            self.assertEqual(
                first["held_out_period"]["study_period_index"], 0)
            held_out_report_path = (
                root / execution.PERIOD_DIRECTORY /
                execution._period_filename(self.manifest.selected_periods[0]))
            held_out_report = json.loads(held_out_report_path.read_text(encoding="utf-8"))
            persisted_v1_minute_times = tuple(
                record["decision_time_ms"] for record in held_out_report["candidate_evidence"]
                if record["experiment_id"] == "V1"
                and record["evidence_kind"] == "CONTINUOUS")
            self.assertIn(
                blocks[0].feature_blocks[0][0].evaluation_boundary_time_ms,
                persisted_v1_minute_times)
            with patch.object(crossfit, "train_hmm_regime_model_from_blocks",
                              side_effect=train), patch.object(
                    crossfit, "filter_hmm_regime_feature_blocks",
                    side_effect=filter_blocks):
                paths_again = crossfit.freeze_development_hmm_crossfit(
                    self.manifest, root, code_revision="crossfit-fixture")
            self.assertEqual(paths_again, paths)
            with patch.object(crossfit, "_parse_hmm_model",
                              side_effect=parse_fixture_model):
                with self.assertRaises(ValueError):
                    crossfit.validate_hmm_crossfit_index(
                        root / crossfit.HMM_CROSSFIT_INDEX_FILENAME, self.manifest,
                        coverage_sha256="b" * 64, code_revision="crossfit-fixture")

            index_path = root / crossfit.HMM_CROSSFIT_INDEX_FILENAME
            index = json.loads(index_path.read_text(encoding="utf-8"))
            first_entry = index["ordered_folds"][0]
            fold_path = root / crossfit.HMM_CROSSFIT_DIRECTORY / first_entry["fold_file"]
            fold = json.loads(fold_path.read_text(encoding="utf-8"))
            fold["ordered_training_blocks"].reverse()
            fold = json.loads(execution._artifact_json(fold, "artifact_sha256"))
            fold_path.write_text(execution._canonical(fold), encoding="utf-8")
            first_entry["artifact_sha256"] = fold["artifact_sha256"]
            index = json.loads(execution._artifact_json(index, "index_sha256"))
            index_path.write_text(execution._canonical(index), encoding="utf-8")
            with patch.object(crossfit, "_parse_hmm_model",
                              side_effect=parse_fixture_model):
                with self.assertRaisesRegex(ValueError, "training identities"):
                    crossfit.validate_hmm_crossfit_index(
                        index_path, self.manifest,
                        coverage_sha256=coverage_sha, code_revision="crossfit-fixture")

    def test_crossfit_rejects_mixed_code_or_coverage_revisions(self):
        for changed_field, changed_value in (("code_revision", "other-revision"),
                                             ("extension_coverage_manifest_sha256", "c" * 64)):
            with self.subTest(field=changed_field), TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, _ = self._write_development_reports(root)
                period = self.manifest.selected_periods[1]
                report_path = root / execution.PERIOD_DIRECTORY / execution._period_filename(period)
                report = json.loads(report_path.read_text(encoding="utf-8"))
                report[changed_field] = changed_value
                report = json.loads(execution._artifact_json(report, "report_sha256"))
                report_path.write_text(execution._canonical(report), encoding="utf-8")
                with self.assertRaises(ValueError):
                    crossfit.freeze_development_hmm_crossfit(
                        self.manifest, root, code_revision="crossfit-fixture")

    def test_crossfit_fails_closed_before_all_ten_reports_exist(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                crossfit.freeze_development_hmm_crossfit(
                    self.manifest, temporary, code_revision="crossfit-fixture")


if __name__ == "__main__":
    unittest.main()
