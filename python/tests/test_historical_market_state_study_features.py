"""Synthetic Part-C schemas, transforms and development-only bin fixtures."""

from dataclasses import FrozenInstanceError, replace
from datetime import date
import unittest

from market_analysis.historical_market_state_study import PRIMARY_HYPOTHESES
from market_analysis import historical_market_state_study_features as features


class StudyFeatureTests(unittest.TestCase):
    def observation(self, family="EXP-75-03", **changes):
        spec = features.predictive_family(family)
        values = dict(study_period_index=0, utc_date=date(2024, 1, 1), phase="development",
                      family_id=family, algorithm_version="synthetic-v1", config_version="config-a",
                      observation_mode=spec.observation_mode, decision_time_ms=1_704_067_200_000,
                      horizon_minutes=spec.horizon_minutes, outcome_id=spec.outcome_id,
                      outcome_kind=spec.outcome_kind, observation_key="row-0",
                      baseline_features=(0.0,) * 15,
                      candidate_features=(0.0,) * len(spec.candidate_features), outcome=0.0)
        values.update(changes)
        return features.AlignedStudyObservation(**values)

    def day(self, observation, **changes):
        values = {name: getattr(observation, name) for name in (
            "study_period_index", "utc_date", "phase", "family_id", "algorithm_version",
            "config_version", "observation_mode", "horizon_minutes", "outcome_id", "outcome_kind")}
        values.update(scheduled_primary_count=2 if observation.observation_mode == "CONTINUOUS" else None,
                      observations=(observation,))
        values.update(changes)
        return features.AlignedStudyDay(**values)

    def test_exact_baseline_order_and_processing(self):
        self.assertEqual(tuple(f.name for f in features.BASELINE_FEATURES), (
            "v1_broad_rise", "v1_broad_drop", "v1_median_normalized_movement",
            "v1_breadth_rising", "v1_breadth_falling", "v1_material_breadth_rising",
            "v1_material_breadth_falling", "v1_dispersion_mad_normalized_movement",
            "v1_median_rvol", "v1_median_acceleration", "v1_pace_accelerating",
            "v1_pace_decelerating", "utc_06_12", "utc_12_18", "utc_18_24"))
        self.assertEqual(tuple(f.processing == "STANDARDIZE" for f in features.BASELINE_FEATURES),
                         (False, False, True, True, True, True, True, True,
                          True, True, False, False, False, False, False))
        self.assertEqual(features.FROZEN_EVALUATION_PLAN.reference_states,
                         ("NEUTRAL", "MIXED", "UTC_00_06", "MID_MOVEMENT"))

    def test_all_families_match_primary_registry_and_modes(self):
        self.assertEqual(len(features.FAMILY_EVALUATION_SPECS), 16)
        self.assertEqual(tuple((s.family_id, s.causal_forward_test, s.horizon_minutes, s.outcome_id)
                               for s in features.FAMILY_EVALUATION_SPECS),
                         tuple((h.family_id, h.causal_forward_test, h.primary_horizon_minutes,
                                h.primary_outcome_id) for h in PRIMARY_HYPOTHESES))
        for spec in features.FAMILY_EVALUATION_SPECS:
            with self.subTest(family=spec.family_id):
                mode = ("RETROSPECTIVE" if spec.family_id == "EXP-75-04A" else
                        "EVENT" if spec.family_id in ("EXP-75-02", "EXP-75-04B") else "CONTINUOUS")
                self.assertEqual(spec.observation_mode, mode)
        for family in ("EXP-75-01", "EXP-75-05", "EXP-75-11-OI", "EXP-75-11-FUNDING"):
            self.assertEqual(features.family_spec(family).outcome_kind, "BINARY")
        self.assertEqual(features.family_spec("EXP-75-11-FUNDING").horizon_minutes, 60)
        self.assertEqual(features.family_spec("EXP-75-11-FUNDING").outcome_id, "v1_direction_persistence")

    def test_exact_candidate_schemas(self):
        expected = (
            (("ewma_minus_raw_normalized_movement", True), ("direction_disagreement", False)),
            (("positive_accumulator", True), ("negative_accumulator", True), ("detector_down_shift", False)),
            (("filtered_minus_raw_normalized_movement", True), ("kalman_trend", True)), (),
            (("recent_change_probability", True),),
            (("candidate_minus_v1_median_acceleration", True),),
            (("candidate_minus_v1_normalized_movement", True), ("direction_disagreement", False)),
            (("candidate_minus_v1_normalized_movement", True), ("direction_disagreement", False)),
            (("explained_variance_ratio", True), ("current_pc1_energy_fraction", True)),
            (("median_pairwise_correlation", True), ("network_edge_density", True)),
            (("hmm_low_movement", False), ("hmm_high_movement", False), ("posterior_entropy", True)),
            (("median_signed_mark_trade_divergence_5m", True),),
            (("median_log_oi_change_5m", True), ("median_log_oi_change_15m", True)),
            (("median_funding_per_hour", True),),
            (("log1p_observed_total_notional_15m", True), ("liquidation_imbalance_15m", True),
             ("liquidation_breadth_15m", True)),
            (("pooled_notional_imbalance_5m", True), ("sign_breadth_5m", True)),
        )
        self.assertEqual(tuple(tuple((f.name, f.processing == "STANDARDIZE")
                                    for f in spec.candidate_features)
                               for spec in features.FAMILY_EVALUATION_SPECS), expected)

    def test_frozen_configuration_cardinalities(self):
        self.assertEqual(tuple(spec.expected_config_count for spec in features.FAMILY_EVALUATION_SPECS),
                         (3, 3, 3, 0, 3, 3, 3, 3, 3, 3, 1, 1, 1, 1, 1, 1))
        self.assertEqual(features.FROZEN_EVALUATION_PLAN.families, features.FAMILY_EVALUATION_SPECS)
        with self.assertRaises(ValueError):
            replace(features.family_spec("EXP-75-03"), expected_config_count=2)
        with self.assertRaises(ValueError):
            replace(features.family_spec("EXP-75-09"), expected_config_count=3)
        with self.assertRaises(ValueError):
            replace(features.FROZEN_EVALUATION_PLAN, families=features.FAMILY_EVALUATION_SPECS[:-1])

    def test_strict_observation_dimensions_finiteness_and_binary_outcomes(self):
        for changes in ({"candidate_features": (1.0,)}, {"baseline_features": (0.0,)},
                        {"candidate_features": (float("nan"), 0.0)}, {"outcome": float("inf")},
                        {"phase": "other"}, {"observation_mode": "EVENT"},
                        {"horizon_minutes": 0}, {"outcome_id": "other"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.observation(**changes)
        with self.assertRaises(ValueError):
            self.observation("EXP-75-01", outcome=0.5)
        with self.assertRaises(ValueError):
            self.observation("EXP-75-02", candidate_features=(0.0, 0.0, 0.2))
        with self.assertRaises(FrozenInstanceError):
            self.observation().outcome = 5

    def test_exact_primary_and_event_boundary_timing(self):
        start = 1_704_067_200_000
        for timestamp in (start + 1, start + 60_000, start - 1, start + 86_400_000):
            with self.subTest(mode="CONTINUOUS", timestamp=timestamp), self.assertRaises(ValueError):
                self.observation(decision_time_ms=timestamp)
        for timestamp in (start + 1, start + 6_000, start - 5_000, start + 86_400_000):
            with self.subTest(mode="EVENT", timestamp=timestamp), self.assertRaises(ValueError):
                self.observation("EXP-75-04B", decision_time_ms=timestamp)
        self.assertEqual(self.observation(decision_time_ms=start + 15 * 60_000).decision_time_ms,
                         start + 15 * 60_000)
        self.assertEqual(self.observation("EXP-75-04B", decision_time_ms=start + 5_000).decision_time_ms,
                         start + 5_000)

    def test_pelt_cannot_create_predictive_observations(self):
        spec = features.family_spec("EXP-75-04A")
        self.assertFalse(spec.causal_forward_test)
        self.assertEqual(spec.candidate_features, ())
        with self.assertRaisesRegex(ValueError, "retrospective"):
            features.predictive_family(spec.family_id)
        with self.assertRaises(ValueError):
            self.observation("EXP-75-04A")

    def test_day_identity_duplicates_and_scheduled_count(self):
        row = self.observation()
        self.assertEqual(self.day(row).observations, (row,))
        for changes in ({"scheduled_primary_count": None}, {"scheduled_primary_count": 0},
                        {"observations": (row, row)},
                        {"observations": (replace(row, config_version="config-b"),)}):
            with self.assertRaises(ValueError):
                self.day(row, **changes)
        event = self.observation("EXP-75-04B")
        with self.assertRaises(ValueError):
            self.day(event, scheduled_primary_count=1)

    def test_outcome_transforms(self):
        self.assertEqual(features.absolute_market_return(-0.02), 0.02)
        self.assertEqual(features.future_breadth_extremity((1, 1, -1, 0)), 0.25)
        self.assertEqual(features.future_breadth_extremity((0, 0)), 0)
        self.assertEqual(tuple(features.persistence_weakening_binary(v)
                               for v in ("PERSISTED", "WEAKENED", "REVERSED")), (1.0, 0.0, 0.0))
        for values in ((), (float("nan"),), (float("inf"),)):
            with self.assertRaises(ValueError):
                features.future_breadth_extremity(values)
        with self.assertRaises(ValueError):
            features.persistence_weakening_binary("UNAVAILABLE")

    def test_day_balanced_terciles_quantiles_ties_and_constant_support(self):
        def day(index, values, phase="development"):
            return features.EvidenceValueDay(index, date(2024, 1, index + 1), phase, values)
        # One busy day has only half the mass, despite its 100 observations.
        bins = features.fit_day_balanced_terciles((day(0, (0,) * 100), day(1, (10, 20))))
        self.assertEqual((bins.q1, bins.q2), (0, 10))
        self.assertEqual(tuple(bins.assign(v) for v in (0, 10, 20)), ("LOW", "MID", "HIGH"))
        tied = features.fit_day_balanced_terciles((day(0, (1, 1, 1, 2)),))
        self.assertEqual((tied.q1, tied.q2, tied.observed_bins), (1, 1, ("LOW", "HIGH")))
        constant = features.fit_day_balanced_terciles((day(0, (7,)), day(1, (7, 7))))
        self.assertEqual(constant.observed_bins, ("LOW",))
        self.assertEqual(constant.assign(7), "LOW")
        exact = features.fit_day_balanced_terciles((day(0, (1, 2, 3)),))
        self.assertEqual((exact.q1, exact.q2), (1, 2))
        with self.assertRaises(ValueError):
            features.fit_day_balanced_terciles((day(0, (1,), "validation"),))
        self.assertEqual(bins, features.fit_day_balanced_terciles((day(1, (10, 20)), day(0, (0,) * 100))))


if __name__ == "__main__":
    unittest.main()
