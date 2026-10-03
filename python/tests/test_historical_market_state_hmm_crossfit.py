"""Generated governance fixtures for #123 development HMM cross-fit artifacts."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from market_analysis.experiments.market_state_hmm_regimes import (
    HMMDevelopmentTrainingBlock,
    HMMFeatureRow,
    HMMTrainingDiagnostics,
)
from market_analysis.historical_experiment_batch import report_json_safe
from market_analysis.historical_market_state_study import (
    parse_historical_market_state_study_manifest_json,
)
import market_analysis.historical_market_state_hmm_crossfit as crossfit
import market_analysis.historical_market_state_study_execution as execution


MANIFEST_PATH = (Path(__file__).parents[2] / "research" /
                 "historical-market-state-study-v1" / "selection" /
                 "historical-market-state-study-v1-manifest.json")


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
                ((row,),), 0)
            blocks.append(block)
            body = {
                "period_report_schema_version": execution.PERIOD_REPORT_SCHEMA_VERSION,
                "study_manifest_sha256": self.manifest.manifest_sha256,
                "extension_coverage_manifest_sha256": coverage_sha,
                "code_revision": revision,
                "period": report_json_safe(period),
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
            diagnostics = HMMTrainingDiagnostics(
                "HMM_TRAINING_READY", None, 9, 0, 9, 0,
                blocks[0].start_boundary_time_ms,
                blocks[-1].start_boundary_time_ms,
                "b" * 64)
            trained_inputs = []

            def train(training_blocks, config):
                training_blocks = tuple(training_blocks)
                trained_inputs.append(tuple(
                    item.study_period_index for item in training_blocks))
                held_out = next(index for index in range(10)
                                if index not in trained_inputs[-1])
                return diagnostics, SimpleNamespace(
                    model_sha256=(f"{held_out:02d}" * 32)[:64])

            def filter_blocks(feature_blocks, model, config):
                return tuple(tuple({
                    "evaluation_boundary_time_ms": row.evaluation_boundary_time_ms,
                    "hard_state": "MID_MOVEMENT",
                    "model_sha256": model.model_sha256,
                } for row in block) for block in feature_blocks)

            with patch.object(crossfit, "train_hmm_regime_model_from_blocks",
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

    def test_crossfit_fails_closed_before_all_ten_reports_exist(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                crossfit.freeze_development_hmm_crossfit(
                    self.manifest, temporary, code_revision="crossfit-fixture")


if __name__ == "__main__":
    unittest.main()
