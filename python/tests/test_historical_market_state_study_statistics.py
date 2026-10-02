"""Synthetic day-level losses, deterministic inference and classification."""

from dataclasses import replace
import hashlib
import unittest
from unittest.mock import Mock, patch
import numpy as np

from market_analysis import historical_market_state_study_statistics as stats


class StudyStatisticsTests(unittest.TestCase):
    def namespace(self):
        return stats.BootstrapNamespace("a" * 64, "test", "EXP-75-03", "config-a", 15,
                                        "signed_market_return")

    def test_mse_brier_and_raw_delta_sign(self):
        self.assertEqual(stats.squared_error(3, 1), 4)
        self.assertEqual(stats.day_loss("CONTINUOUS", (1, 3), (1, 1)), 2)
        self.assertAlmostEqual(stats.brier_loss(1, 0.75), 0.0625)
        self.assertAlmostEqual(stats.day_loss("BINARY", (0, 1), (0.25, 0.75)), 0.0625)
        self.assertEqual(stats.incremental_effect(3, 1), 2)
        self.assertEqual(stats.incremental_effect(1, 3), -2)
        self.assertIsNone(stats.relative_loss_improvement(0, 1))
        self.assertEqual(stats.relative_loss_improvement(4, 2), 0.5)
        for actual, prediction in ((0.5, 0.1), (1, -0.1), (0, 1.1)):
            with self.assertRaises(ValueError):
                stats.brier_loss(actual, prediction)
        with self.assertRaises(ValueError):
            stats.day_loss("CONTINUOUS", (), ())
        with self.assertRaises(ValueError):
            stats.squared_error(float("nan"), 0)

    def test_seed_namespace_exact_128_bits_and_deterministic_10000_draws(self):
        namespace = self.namespace()
        text = ("crypto-watcher:historical-market-state-study-v1:part-c-v1:" + "a" * 64
                + ":test:EXP-75-03:config-a:15:signed_market_return:median_delta")
        expected = int.from_bytes(hashlib.sha256(text.encode()).digest()[:16], "big")
        self.assertEqual(namespace.seed, expected)
        first = stats.whole_day_bootstrap((-1, 2, 4), namespace)
        second = stats.whole_day_bootstrap((-1, 2, 4), namespace)
        self.assertEqual(first, second)
        self.assertEqual(first.draws, 10_000)
        self.assertEqual(first.seed, expected)
        self.assertEqual(first.generator, "numpy.random.PCG64")
        self.assertEqual(first.quantile_method, "linear")
        self.assertEqual((first.median, first.day_count, first.positive_day_count), (2, 3, 2))
        self.assertEqual(first.positive_day_fraction, 2 / 3)
        self.assertNotEqual(replace(namespace, config_version="config-b").seed, expected)
        self.assertNotEqual(replace(namespace, statistic="mean_delta").seed, expected)

    def test_bootstrap_resamples_only_whole_day_effects(self):
        namespace = self.namespace()
        generator = Mock(wraps=np.random.Generator(np.random.PCG64(namespace.seed)))
        with patch.object(stats.np.random, "Generator", return_value=generator):
            result = stats.whole_day_bootstrap((2, 2, 2), namespace)
        generator.integers.assert_called_once_with(0, 3, size=(10_000, 3))
        self.assertEqual(result.median_ci95, (2, 2))
        self.assertEqual(result.mean_ci95, (2, 2))
        with self.assertRaises(ValueError):
            stats.whole_day_bootstrap(((1, 2), (3,)), namespace)
        with self.assertRaises(ValueError):
            stats.whole_day_bootstrap((), namespace)

    def test_exact_one_sided_sign_test_small_cases_and_zero_ties(self):
        self.assertEqual(stats.exact_day_sign_test((1, 1, 1)).raw_p, 1 / 8)
        result = stats.exact_day_sign_test((1, 1, -1, 0))
        self.assertEqual((result.nonzero_day_count, result.positive_day_count, result.raw_p), (3, 2, 0.5))
        self.assertEqual(stats.exact_day_sign_test((-1, -1)).raw_p, 1)
        empty = stats.exact_day_sign_test((0, 0))
        self.assertEqual((empty.status, empty.raw_p), ("NO_NONZERO_DAYS", 1))

    def test_holm_known_example_fixed_membership_missing_and_vetoed(self):
        results = stats.holm_adjust(("a", "b", "c"), (
            stats.HypothesisPValue("a", 0.01), stats.HypothesisPValue("b", 0.04),
            stats.HypothesisPValue("c", 0.03)))
        self.assertEqual(tuple(r.hypothesis_id for r in results), ("a", "b", "c"))
        for result, expected in zip(results, (0.03, 0.06, 0.06)):
            self.assertAlmostEqual(result.adjusted_p, expected)
        partial = stats.holm_adjust(("a", "b", "c", "d"), (
            stats.HypothesisPValue("a", 0.01), stats.HypothesisPValue("b", 0.01),
            stats.HypothesisPValue("c", 0.001, "VETOED")))
        self.assertEqual(tuple(r.raw_p for r in partial), (0.01, 0.01, 1, 1))
        self.assertEqual(tuple(r.adjusted_p for r in partial), (0.04, 0.04, 1, 1))
        self.assertEqual(partial[-1].status, "MISSING")
        with self.assertRaises(ValueError):
            stats.holm_adjust(("a",), (stats.HypothesisPValue("other", 0.1),))

    def test_evidence_classification_precedence(self):
        classification = stats.EvidenceClassification
        robust = stats.EvidenceComponents("ADEQUATE", "FEASIBLE", 0.1, 0.2, 0.3, (0.01, 0.5))
        self.assertEqual(stats.classify_evidence(robust), classification.ROBUST_INCREMENTAL_EVIDENCE)
        self.assertEqual(stats.classify_evidence(replace(robust, coverage_status="COVERAGE_LIMITED")),
                         classification.COVERAGE_LIMITED)
        self.assertEqual(stats.classify_evidence(replace(robust, model_status="FIT_FAILED")),
                         classification.COVERAGE_LIMITED)
        self.assertEqual(stats.classify_evidence(replace(robust, validation_median=0, track_a_supported=True)),
                         classification.UNSTABLE_ACROSS_PERIODS)
        self.assertEqual(stats.classify_evidence(replace(robust, test_median=-1)),
                         classification.UNSTABLE_ACROSS_PERIODS)
        inconclusive = replace(robust, test_median_ci95=(-0.1, 0.5))
        self.assertEqual(stats.classify_evidence(inconclusive), classification.INCONCLUSIVE)
        self.assertEqual(stats.classify_evidence(replace(inconclusive, track_a_supported=True,
                                                       exact_information_redundancy=True)),
                         classification.STATE_QUALITY_ONLY)
        self.assertEqual(stats.classify_evidence(replace(inconclusive, exact_information_redundancy=True)),
                         classification.REDUNDANT_WITH_V1)
        self.assertEqual(stats.classify_evidence(replace(robust, phase_sign_reversal=True)),
                         classification.INCONCLUSIVE)

    def test_non_significance_never_establishes_redundancy(self):
        components = stats.EvidenceComponents("ADEQUATE", "FEASIBLE", 0, 0, 0, (-1, 1))
        self.assertEqual(stats.classify_evidence(components), stats.EvidenceClassification.INCONCLUSIVE)
        self.assertNotEqual(stats.classify_evidence(components), stats.EvidenceClassification.REDUNDANT_WITH_V1)


if __name__ == "__main__":
    unittest.main()
