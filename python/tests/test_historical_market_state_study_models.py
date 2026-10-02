"""Small synthetic numerical fixtures; no study artifacts or market execution."""

from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from market_analysis.historical_market_state_study_features import FeatureSpec, canonical_json
from market_analysis import historical_market_state_study_models as models


class StudyModelTests(unittest.TestCase):
    def test_day_balancing_and_population_scaling(self):
        weights = models.day_balanced_weights((2, 4))
        self.assertAlmostEqual(sum(weights[:2]), 0.5)
        self.assertAlmostEqual(sum(weights[2:]), 0.5)
        schema = (FeatureSpec("value", "STANDARDIZE"), FeatureSpec("flag", "BINARY"))
        fitted = models.fit_standardizer(schema, (((0, 0), (2, 1)), ((10, 1),) * 4))
        self.assertAlmostEqual(fitted.means[0], 5.5)
        self.assertAlmostEqual(fitted.scales[0] ** 2, 20.75)
        self.assertEqual((fitted.means[1], fitted.scales[1]), (0, 1))
        self.assertEqual(fitted.transform((5.5, 1)), (0, 1))
        with self.assertRaises(models.StudyModelFitError):
            models.day_balanced_weights((1, 0))

    def test_training_only_scaling_and_zero_variance_support(self):
        schema = (FeatureSpec("constant", "STANDARDIZE"), FeatureSpec("value", "STANDARDIZE"))
        training = (((4, 1), (4, 3)),)
        fitted = models.fit_standardizer(schema, training)
        before = canonical_json(fitted)
        transformed = fitted.transform((1e100, 1e100))
        self.assertEqual(transformed[0], 0)
        self.assertEqual((fitted.means, fitted.scales), ((4, 2), (1, 1)))
        self.assertEqual(fitted.active_mask, (False, True))
        self.assertEqual(fitted.support_reasons, ("ZERO_VARIANCE", "ACTIVE"))
        self.assertEqual(fitted.active_feature_names, ("value",))
        self.assertEqual(canonical_json(fitted), before)
        rows = tuple(fitted.active_row(row) for row in training[0])
        model = models.fit_weighted_ols(fitted.active_feature_names, rows, (1, 3), (0.5, 0.5))
        self.assertEqual(model.feature_names, ("intercept", "value"))

    def test_weighted_ols_known_solution_and_predictions_do_not_mutate(self):
        fitted = models.fit_weighted_ols(("x",), ((0,), (1,), (2,)), (1, 3, 5), (0.2, 0.3, 0.5))
        self.assertAlmostEqual(fitted.coefficients[0], 1)
        self.assertAlmostEqual(fitted.coefficients[1], 2)
        self.assertEqual(fitted.rank, 2)
        self.assertEqual(fitted.rcond, 1e-12)
        before = canonical_json(fitted)
        self.assertAlmostEqual(fitted.predict(((3,),))[0], 7)
        self.assertEqual(canonical_json(fitted), before)
        changed = replace(fitted, coefficients=(2, 2))
        self.assertNotEqual(changed.model_sha256, fitted.model_sha256)

    def test_rank_deficient_ols_fails(self):
        with self.assertRaisesRegex(models.StudyModelFitError, "rank-deficient"):
            models.fit_weighted_ols(("x", "duplicate"), ((1, 1), (2, 2), (3, 3)),
                                    (1, 2, 3), (1, 1, 1))
        with self.assertRaises(ValueError):
            models.fit_weighted_ols(("x",), ((float("inf"),),), (1,), (1,))

    def test_logistic_intercept_and_group_probabilities(self):
        fitted = models.fit_weighted_logistic((), ((),) * 4, (0, 1, 1, 1), (1,) * 4)
        self.assertAlmostEqual(fitted.coefficients[0], math.log(3), places=6)
        self.assertAlmostEqual(fitted.predict(((),))[0], 0.75, places=7)
        grouped = models.fit_weighted_logistic(("group",), ((0,),) * 4 + ((1,),) * 4,
                                               (0, 0, 0, 1, 0, 1, 1, 1), (1,) * 8)
        self.assertAlmostEqual(grouped.coefficients[0], -math.log(3), places=6)
        self.assertAlmostEqual(grouped.coefficients[1], 2 * math.log(3), places=6)
        before = canonical_json(grouped)
        predictions = grouped.predict(((0,), (1,), (1e6,), (-1e6,)))
        self.assertAlmostEqual(predictions[0], 0.25, places=7)
        self.assertAlmostEqual(predictions[1], 0.75, places=7)
        self.assertTrue(all(0 <= p <= 1 for p in predictions))
        self.assertEqual(canonical_json(grouped), before)

    def test_logistic_one_class_separation_and_rank_failure_are_explicit(self):
        with self.assertRaisesRegex(models.StudyModelFitError, "both exact binary classes"):
            models.fit_weighted_logistic((), ((),) * 3, (1, 1, 1), (1,) * 3)
        with self.assertRaisesRegex(models.StudyModelFitError, "separation"):
            models.fit_weighted_logistic(("x",), ((-2,), (-1,), (1,), (2,)), (0, 0, 1, 1), (1,) * 4)
        with self.assertRaisesRegex(models.StudyModelFitError, "rank-deficient"):
            models.fit_weighted_logistic(("x",), ((1,),) * 4, (0, 0, 1, 1), (1,) * 4)
        with patch.object(models, "LOGISTIC_MAX_ITERATIONS", 0):
            with self.assertRaisesRegex(models.StudyModelFitError, "did not converge"):
                models.fit_weighted_logistic((), ((),) * 4, (0, 1, 1, 1), (1,) * 4)


if __name__ == "__main__":
    unittest.main()
