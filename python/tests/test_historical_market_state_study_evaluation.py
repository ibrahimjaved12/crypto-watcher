"""Synthetic aligned days only; never reads real study periods or artifacts."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import json
import unittest
from unittest.mock import patch

from market_analysis import historical_market_state_study_evaluation as evaluation
from market_analysis.historical_market_state_study_features import (
    AlignedStudyDay, AlignedStudyObservation, BASELINE_FEATURES,
    FROZEN_EVALUATION_PLAN, canonical_json, predictive_family,
)


IDENTITY = evaluation.CandidateConfigIdentity("EXP-75-03", "synthetic-kalman-v1", "config-a")


def synthetic_day(index, phase="development", config="config-a", count=32):
    """Independent basis columns give both designs full rank without randomness."""
    day_date = date(2024, 1, 1) + timedelta(days=index)
    start = int(datetime.combine(day_date, datetime.min.time(), timezone.utc).timestamp()) * 1000
    rows = []
    for row_index in range(count):
        baseline = tuple(1.0 if row_index == column + 1 else 0.0 for column in range(15))
        candidate = (1.0 if row_index == 16 else 0.0, 1.0 if row_index == 17 else 0.0)
        outcome = 2 + 0.3 * baseline[2] + 1.5 * candidate[0] - 0.7 * candidate[1] + index * 0.01
        rows.append(AlignedStudyObservation(index, day_date, phase, IDENTITY.family_id,
                                             IDENTITY.algorithm_version, config, "CONTINUOUS",
                                             start + row_index * 15 * 60_000, 15,
                                             "signed_market_return", "CONTINUOUS", f"{row_index:02}",
                                             baseline, candidate, outcome))
    return AlignedStudyDay(index, day_date, phase, IDENTITY.family_id, IDENTITY.algorithm_version,
                           config, "CONTINUOUS", 15, "signed_market_return", "CONTINUOUS", count, tuple(rows))


def synthetic_event_day(index, config, empty=False):
    source = synthetic_day(index, config=config)
    spec = predictive_family("EXP-75-04B")
    row = source.observations[0]
    event = replace(row, family_id=spec.family_id, algorithm_version="synthetic-bocpd-v1",
                    observation_mode="EVENT", horizon_minutes=60, outcome_id="realized_volatility",
                    candidate_features=(0.1,), observation_key="event-" + config,
                    decision_time_ms=row.decision_time_ms + {"a": 1_000, "b": 5_000, "c": 9_000}[config])
    return AlignedStudyDay(index, source.utc_date, "development", spec.family_id, event.algorithm_version,
                           config, "EVENT", 60, "realized_volatility", "CONTINUOUS", None,
                           () if empty else (event,))


def fixed_identities(identity=IDENTITY):
    count = predictive_family(identity.family_id).expected_config_count
    configs = (identity.config_version,) if count == 1 else ("config-a", "config-b", "config-c")
    return tuple(replace(identity, config_version=config) for config in configs)


def family_days(identity, days):
    return tuple(replace(day, config_version=config.config_version, observations=tuple(
        replace(row, config_version=config.config_version) for row in day.observations))
        for config in fixed_identities(identity) for day in days)


def evaluate_config_fixture(identity, days):
    common = evaluation.common_development_samples(fixed_identities(identity), family_days(identity, days))
    sample = next(s for s in common.samples if s.identity == identity)
    return evaluation.evaluate_development_config(identity, sample.days, common.samples_sha256)


@lru_cache(maxsize=1)
def development_fixture():
    days = tuple(synthetic_day(index) for index in range(8))
    common, results, nomination = evaluation.evaluate_development_family(
        fixed_identities(), family_days(IDENTITY, days))
    result = next(r for r in results if r.identity == IDENTITY)
    pair = evaluation.fit_final_development_pair(nomination, days)
    freeze = evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (pair,), (common,))
    return days, result, nomination, pair, freeze


class StudyEvaluationTests(unittest.TestCase):
    def test_lodo_whole_day_folds_local_preprocessing_and_paired_losses(self):
        days = tuple(synthetic_day(index) for index in range(8))
        first = days[0]
        row = first.observations[3]
        extreme = replace(row, baseline_features=row.baseline_features[:2] + (1e3,) + row.baseline_features[3:])
        days = (replace(first, observations=first.observations[:3] + (extreme,) + first.observations[4:]),) + days[1:]
        with patch.object(evaluation, "day_loss", wraps=evaluation.day_loss) as losses:
            result = evaluate_config_fixture(IDENTITY, days)
        self.assertEqual(result.status, "EVALUABLE")
        self.assertEqual(len(result.folds), 8)
        self.assertEqual(len(losses.call_args_list), 16)
        for index, fold in enumerate(result.folds):
            self.assertNotIn(fold.held_out_period, fold.training_periods)
            self.assertEqual(fold.training_periods, tuple(i for i in range(8) if i != index))
            self.assertEqual(fold.day_result.observation_count, 32)
            self.assertEqual(fold.day_result.observation_keys,
                             tuple(r.observation_key for r in days[index].observations))
            baseline_call, extended_call = losses.call_args_list[2 * index:2 * index + 2]
            self.assertEqual(baseline_call.args[1], extended_call.args[1])
            self.assertEqual(len(baseline_call.args[2]), len(extended_call.args[2]))
        # Held-out extreme evidence cannot enter its training mean.
        self.assertAlmostEqual(result.folds[0].baseline_preprocessing.means[2], 1 / 32)
        self.assertGreater(result.folds[1].baseline_preprocessing.means[2], 1)

    def test_continuous_configs_use_exact_common_rows_before_day_eligibility(self):
        a = tuple(synthetic_day(i, config="a") for i in range(8))
        b = tuple(replace(synthetic_day(i, config="b"), observations=synthetic_day(i, config="b").observations[8:])
                  for i in range(8))
        c = tuple(synthetic_day(i, config="c") for i in range(8))
        identities = tuple(replace(IDENTITY, config_version=config) for config in ("a", "b", "c"))
        common = evaluation.common_development_samples(identities, a + b + c)
        self.assertEqual(common.excluded_days, ())
        self.assertEqual(tuple(len(s.days) for s in common.samples), (8, 8, 8))
        for a_day, b_day in zip(common.samples[0].days, common.samples[1].days):
            self.assertEqual(tuple(r.observation_key for r in a_day.observations),
                             tuple(r.observation_key for r in b_day.observations))
            self.assertEqual(len(a_day.observations), 24)
        self.assertEqual(tuple(d.observations for d in common.samples[0].days),
                         tuple(tuple(replace(row, config_version="a") for row in d.observations)
                               for d in common.samples[2].days))
        # Individually adequate disjoint populations must not nominate a winner.
        disjoint_a = tuple(replace(d, observations=d.observations[:16]) for d in a)
        disjoint_b = tuple(replace(synthetic_day(i, config="b"), observations=synthetic_day(i, config="b").observations[16:])
                           for i in range(8))
        common, results, nomination = evaluation.evaluate_development_family(identities, disjoint_a + disjoint_b + c)
        self.assertTrue(all(not sample.days for sample in common.samples))
        self.assertTrue(all(r.status == "COVERAGE_LIMITED" and r.median_delta is None for r in results))
        self.assertEqual(nomination.status, "NOT_EVALUABLE")

    def test_common_keys_cannot_join_different_outcomes_or_baseline_contexts(self):
        a, b, c = (synthetic_day(0, config=config) for config in ("a", "b", "c"))
        bad = replace(b.observations[0], outcome=99)
        identities = tuple(replace(IDENTITY, config_version=config) for config in ("a", "b", "c"))
        with self.assertRaisesRegex(ValueError, "different baseline/time/outcome"):
            evaluation.common_development_samples(identities, (a, replace(b, observations=(bad,) + b.observations[1:]), c))

    def test_event_configs_share_days_and_keep_distinct_native_events(self):
        identities = tuple(evaluation.CandidateConfigIdentity("EXP-75-04B", "synthetic-bocpd-v1", config)
                           for config in ("a", "b", "c"))
        a = tuple(synthetic_event_day(i, "a") for i in range(10))
        b = tuple(synthetic_event_day(i, "b", empty=i == 2) for i in range(10))
        c = tuple(synthetic_event_day(i, "c") for i in range(10))
        common = evaluation.common_development_samples(identities, a + b + c)
        self.assertEqual(tuple(len(s.days) for s in common.samples), (9, 9, 9))
        self.assertEqual(common.excluded_days[0].study_period_index, 2)
        for selection in common.selection_provenance:
            self.assertIsNone(selection.common_observation_keys)
            self.assertEqual(selection.common_day_eligible, selection.study_period_index != 2)
            self.assertEqual(tuple(item.common_usable_row_count for item in selection.supplied_configs),
                             (1, 0, 1) if selection.study_period_index == 2 else (1, 1, 1))
        event_common, results, nomination = evaluation.evaluate_development_family(identities, a[:1] + b[:1] + c[:1])
        self.assertEqual(nomination.status, "NOT_EVALUABLE")
        evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (), (event_common,))
        for a_day, b_day in zip(common.samples[0].days, common.samples[1].days):
            self.assertEqual(a_day.study_period_index, b_day.study_period_index)
            self.assertNotEqual(a_day.observations[0].observation_key, b_day.observations[0].observation_key)
            self.assertNotEqual(a_day.observations[0].decision_time_ms, b_day.observations[0].decision_time_ms)

    def test_common_selection_freezes_complete_source_population_and_dispositions(self):
        identities = tuple(replace(IDENTITY, config_version=config) for config in ("a", "b", "c"))
        missing_a = synthetic_day(0, config="a")
        missing_b = synthetic_day(0, config="b")
        sparse_a = replace(synthetic_day(1, config="a"),
                           observations=synthetic_day(1, config="a").observations[:16])
        sparse_b = replace(synthetic_day(1, config="b"),
                           observations=synthetic_day(1, config="b").observations[1:17])
        sparse_c = replace(synthetic_day(1, config="c"),
                           observations=synthetic_day(1, config="c").observations[2:18])
        retained_days = tuple(synthetic_day(2, config=config) for config in ("a", "b", "c"))
        source_days = (missing_a, missing_b, sparse_a, sparse_b, sparse_c) + retained_days
        common = evaluation.common_development_samples(identities, source_days)
        missing, failed, retained = common.selection_provenance
        self.assertEqual(retained.disposition, "RETAINED")
        self.assertEqual(tuple(item.common_usable_row_count for item in retained.supplied_configs), (32, 32, 32))
        self.assertEqual(missing.disposition, "MISSING_CONFIG_DAY")
        self.assertEqual(tuple(item.identity.config_version for item in missing.supplied_configs), ("a", "b"))
        self.assertEqual(failed.disposition, "COMMON_DAY_COVERAGE_FAILED")
        self.assertEqual(failed.common_observation_keys, tuple(f"{i:02}" for i in range(2, 16)))
        self.assertEqual(tuple(item.common_usable_row_count for item in failed.supplied_configs), (14, 14, 14))
        self.assertEqual(tuple(item.source_row_count for item in failed.supplied_configs), (16, 16, 16))
        self.assertEqual(tuple(item.study_period_index for item in common.selection_provenance), (0, 1, 2))
        self.assertEqual(tuple(item.reason for item in common.excluded_days),
                         ("MISSING_CONFIG_DAY", "COMMON_DAY_COVERAGE_FAILED"))
        reordered = evaluation.common_development_samples(identities, tuple(reversed(source_days)))
        self.assertEqual(common.source_days_sha256, reordered.source_days_sha256)
        self.assertEqual(common.samples_sha256, reordered.samples_sha256)
        changed_row = replace(missing_a.observations[0], candidate_features=(2.0, 0.0))
        changed_a = replace(missing_a, observations=(changed_row,) + missing_a.observations[1:])
        changed = evaluation.common_development_samples(identities,
                                                       (changed_a, missing_b, sparse_a, sparse_b, sparse_c) + retained_days)
        self.assertNotEqual(common.source_days_sha256, changed.source_days_sha256)
        self.assertEqual(common.samples, changed.samples)
        _, results, nomination = evaluation.evaluate_development_family(identities, source_days)
        freeze = evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (), (common,))
        with self.assertRaises(ValueError):
            replace(freeze, common_samples=(changed,))
        with self.assertRaises(ValueError):
            replace(common, source_days_sha256="b" * 64)
        with self.assertRaises(ValueError):
            replace(common, selection_provenance=(failed, retained))
        with self.assertRaises(ValueError):
            replace(common, selection_provenance=(missing, failed, retained, retained))
        with self.assertRaises(ValueError):
            replace(common, excluded_days=common.excluded_days[1:])
        with self.assertRaises(ValueError):
            replace(common, excluded_days=common.excluded_days +
                    (evaluation.ExcludedDevelopmentDay(3, "MISSING_CONFIG_DAY"),))
        with self.assertRaises(ValueError):
            replace(retained, common_observation_keys=retained.common_observation_keys[:-1])
        with self.assertRaises(ValueError):
            altered = replace(retained, supplied_configs=(
                replace(retained.supplied_configs[0], source_day_sha256="b" * 64),
                *retained.supplied_configs[1:]))
            replace(common, selection_provenance=(missing, failed, altered))
        with self.assertRaises(ValueError):
            replace(common, selection_provenance=(replace(missing, supplied_configs=missing.supplied_configs[:1]),
                                                 failed, retained))
        with self.assertRaises(ValueError):
            replace(common, source_population=common.source_population[:-1])
        with self.assertRaises(ValueError):
            replace(failed, common_observation_keys=failed.common_observation_keys[:-1])
        with self.assertRaises(ValueError):
            replace(common, excluded_days=(replace(common.excluded_days[0], reason="COMMON_DAY_COVERAGE_FAILED"),
                                            common.excluded_days[1]))

    def test_continuous_half_coverage_boundary_and_phase_headline_gates(self):
        day = synthetic_day(0)
        self.assertEqual(evaluation.day_eligibility(replace(day, observations=day.observations[:16])).status, "ELIGIBLE")
        self.assertEqual(evaluation.day_eligibility(replace(day, observations=day.observations[:15])).status, "COVERAGE_LIMITED")
        odd = replace(day, scheduled_primary_count=33, observations=day.observations[:16])
        self.assertEqual(evaluation.day_eligibility(odd).required_count, 17)
        self.assertEqual(evaluation.day_eligibility(odd).status, "COVERAGE_LIMITED")
        for phase, minimum, start, expected in (("development", 8, 0, 10),
                                                ("validation", 6, 10, 8), ("test", 9, 18, 12)):
            days = tuple(synthetic_day(i, phase) for i in range(start, start + minimum))
            with self.subTest(phase=phase):
                good = evaluation.phase_coverage(days, phase)
                self.assertEqual((good.status, good.scheduled_day_count), ("ADEQUATE", expected))
                self.assertEqual(evaluation.phase_coverage(days[:-1], phase).status, "COVERAGE_LIMITED")

    def test_development_rejects_other_phases_and_reports_fit_failure(self):
        with self.assertRaises(ValueError):
            evaluation.evaluate_development_config(IDENTITY, (synthetic_day(10, "validation"),), "a" * 64)
        days = tuple(synthetic_day(i) for i in range(8))
        # Constant binary columns have no independent design support.
        unsupported = tuple(replace(day, observations=tuple(
            replace(row, baseline_features=(0.0,) * 15) for row in day.observations)) for day in days)
        result = evaluate_config_fixture(IDENTITY, unsupported)
        self.assertEqual(result.status, "FIT_FAILED")
        self.assertIsNone(result.median_delta)
        self.assertTrue(all(fold.day_result is None for fold in result.folds))
        with self.assertRaises(ValueError):
            evaluation.CandidateConfigIdentity("EXP-75-04A", "pelt", "config-a")

    def test_real_binary_lodo_uses_logistic_models_and_brier_loss(self):
        identity = evaluation.CandidateConfigIdentity("EXP-75-11-OI", "synthetic-oi-v1", "config-a")
        days = []
        for index in range(8):
            source = synthetic_day(index)
            start = source.observations[0].decision_time_ms
            rows = tuple(replace(row, family_id=identity.family_id, algorithm_version=identity.algorithm_version,
                                 horizon_minutes=15, outcome_id="v1_direction_persistence", outcome_kind="BINARY",
                                 decision_time_ms=start + (2 * row_index + target) * 15 * 60_000,
                                 observation_key=f"{row_index:02}-{target}",
                                 candidate_features=row.candidate_features, outcome=float(target))
                         for row_index, row in enumerate(source.observations) for target in (0, 1))
            days.append(AlignedStudyDay(index, source.utc_date, "development", identity.family_id,
                                         identity.algorithm_version, identity.config_version, "CONTINUOUS",
                                         15, "v1_direction_persistence", "BINARY", 64, rows))
        common, results, nomination = evaluation.evaluate_development_family((identity,), tuple(days))
        result = results[0]
        self.assertEqual(result.status, "EVALUABLE")
        self.assertEqual(result.median_delta, 0)
        self.assertTrue(all(f.day_result.baseline_loss == 0.25 and f.day_result.extended_loss == 0.25
                            for f in result.folds))
        pair = evaluation.fit_final_development_pair(nomination, common.samples[0].days)
        self.assertIsInstance(pair.baseline_model, evaluation.FrozenLogisticModel)
        self.assertIsInstance(pair.extended_model, evaluation.FrozenLogisticModel)

    def test_nomination_largest_raw_median_and_exact_tie_for_complete_family(self):
        _, result, nomination, _, _ = development_fixture()
        self.assertEqual(nomination.identity, IDENTITY)

        def scored(config, score):
            folds = tuple(replace(fold, day_result=replace(fold.day_result,
                          baseline_loss=2 + score, extended_loss=2, delta=score)) for fold in result.folds)
            return replace(result, identity=replace(IDENTITY, config_version=config), folds=folds)

        a, b, c = scored("a", 1.0), scored("b", 1.0), scored("c", 0.5)
        self.assertEqual(evaluation.nominate_configuration((b, c, a)).identity.config_version, "a")
        slightly_better = scored("b", 1.0 + 1e-12)
        self.assertEqual(evaluation.nominate_configuration((a, c, slightly_better)).identity.config_version, "b")
        self.assertEqual(evaluation.nominate_configuration((scored("a", 0.25), c, b)).identity.config_version, "b")

    def test_final_fit_binds_nominated_common_sample_and_only_development(self):
        days, _, nomination, pair, _ = development_fixture()
        self.assertEqual(pair.training_periods, tuple((d.study_period_index, d.utc_date) for d in days))
        self.assertEqual(pair.baseline_preprocessing.features, BASELINE_FEATURES)
        self.assertEqual(pair.plan_sha256, FROZEN_EVALUATION_PLAN.plan_sha256)
        with self.assertRaisesRegex(ValueError, "rows do not match"):
            evaluation.fit_final_development_pair(nomination, (replace(days[0], observations=days[0].observations[:-1]),) + days[1:])
        with self.assertRaises(ValueError):
            evaluation.fit_final_development_pair(nomination, tuple(synthetic_day(i, "validation") for i in range(10, 16)))

    def test_complete_family_cardinality_and_algorithm_identity_fail_closed(self):
        _, _, nomination, pair, development = development_fixture()
        identities = fixed_identities()
        results = development.config_results
        common = development.common_samples[0]
        self.assertEqual(len(common.samples), 3)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r.common_samples_sha256 == common.samples_sha256 for r in results))
        self.assertEqual(nomination.common_samples_sha256, common.samples_sha256)
        self.assertEqual(pair.common_samples_sha256, common.samples_sha256)
        invalid_identities = (
            identities[:-1], identities + (replace(IDENTITY, config_version="extra"),),
            (identities[0], identities[0], identities[2]),
            (identities[0], replace(identities[1], algorithm_version="other-algorithm"), identities[2]),
            (identities[0], replace(identities[1], family_id="EXP-75-07"), identities[2]),
        )
        invalid_results = (
            results[:-1], results + (replace(results[0], identity=replace(IDENTITY, config_version="extra")),),
            (results[0], results[0], results[2]),
            (results[0], replace(results[1], identity=replace(results[1].identity, algorithm_version="other-algorithm")), results[2]),
            (results[0], replace(results[1], identity=replace(results[1].identity, family_id="EXP-75-07")), results[2]),
        )
        for configs, config_results in zip(invalid_identities, invalid_results):
            with self.subTest(configs=configs):
                with self.assertRaises(ValueError):
                    evaluation.common_development_samples(configs, ())
                with self.assertRaises(ValueError):
                    evaluation.nominate_configuration(config_results)
                with self.assertRaises(ValueError):
                    replace(development, config_results=config_results)

    def test_actual_singleton_uses_the_same_common_sample_chain(self):
        identity = evaluation.CandidateConfigIdentity("EXP-75-10", "synthetic-mark-trade-v1", "only-config")
        days = []
        for index in range(8):
            source = synthetic_day(index)
            rows = tuple(replace(row, family_id=identity.family_id, algorithm_version=identity.algorithm_version,
                                 config_version=identity.config_version, horizon_minutes=5,
                                 candidate_features=(row.candidate_features[0],)) for row in source.observations)
            days.append(AlignedStudyDay(index, source.utc_date, "development", identity.family_id,
                                        identity.algorithm_version, identity.config_version, "CONTINUOUS",
                                        5, "signed_market_return", "CONTINUOUS", 32, rows))
        common, results, nomination = evaluation.evaluate_development_family((identity,), tuple(days))
        self.assertEqual(results[0].status, "EVALUABLE")
        self.assertEqual(nomination.identity, identity)
        self.assertEqual(nomination.common_samples_sha256, common.samples_sha256)
        self.assertEqual(results[0].common_samples_sha256, common.samples_sha256)
        pair = evaluation.fit_final_development_pair(nomination, common.samples[0].days)
        freeze = evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (pair,), (common,))
        self.assertIsNone(freeze.hmm_cross_fit_sha256)
        self.assertIsNone(freeze.hmm_final_model_sha256)
        with self.assertRaises(ValueError):
            evaluation.common_development_samples((identity, replace(identity, config_version="extra")), ())

    def test_different_common_sample_shas_cannot_nominate_or_freeze_together(self):
        _, _, _, _, development = development_fixture()
        results = development.config_results
        different = replace(results[1], common_samples_sha256="b" * 64)
        mixed = (results[0], different, results[2])
        with self.assertRaisesRegex(ValueError, "different family common-sample SHAs"):
            evaluation.nominate_configuration(mixed)
        with self.assertRaisesRegex(ValueError, "different family common-sample SHAs"):
            replace(development, config_results=mixed)
        # A uniform arbitrary SHA is also insufficient: freeze must bind the
        # actual common-sample family object, not just matching result strings.
        arbitrary = tuple(replace(result, common_samples_sha256="b" * 64) for result in results)
        nomination = evaluation.nominate_configuration(arbitrary)
        pair = replace(development.predictive_pairs[0], nomination_sha256=nomination.nomination_sha256,
                       common_samples_sha256=nomination.common_samples_sha256)
        with self.assertRaisesRegex(ValueError, "common-sample identity"):
            replace(development, config_results=arbitrary, nominations=(nomination,), predictive_pairs=(pair,))
        with self.assertRaises(ValueError):
            replace(development, common_samples=())

    def test_freeze_checks_per_config_rows_against_the_common_family_object(self):
        _, _, _, _, development = development_fixture()
        results = tuple(replace(result, development_sample_sha256="b" * 64)
                        for result in development.config_results)
        nomination = evaluation.nominate_configuration(results)
        pair = replace(development.predictive_pairs[0], nomination_sha256=nomination.nomination_sha256,
                       development_sample_sha256=nomination.development_sample_sha256)
        with self.assertRaisesRegex(ValueError, "common-sample rows/coverage"):
            replace(development, config_results=results, nominations=(nomination,), predictive_pairs=(pair,))

    def test_hmm_identities_are_required_at_development_freeze(self):
        identity = evaluation.CandidateConfigIdentity("EXP-75-09", "synthetic-hmm-v1", "only-config")
        common, results, nomination = evaluation.evaluate_development_family((identity,), ())
        for cross_fit, final_model in ((None, None), ("b" * 64, None), (None, "c" * 64),
                                       ("invalid", "c" * 64)):
            with self.subTest(cross_fit=cross_fit, final_model=final_model), self.assertRaises(ValueError):
                evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (), (common,),
                                             hmm_cross_fit_sha256=cross_fit, hmm_final_model_sha256=final_model)
        freeze = evaluation.DevelopmentFreeze("a" * 64, results, (nomination,), (), (common,),
                                               hmm_cross_fit_sha256="b" * 64, hmm_final_model_sha256="c" * 64)
        self.assertEqual(freeze.hmm_cross_fit_sha256, "b" * 64)
        self.assertEqual(freeze.hmm_final_model_sha256, "c" * 64)
        non_hmm = development_fixture()[-1]
        self.assertIsNone(non_hmm.hmm_cross_fit_sha256)
        self.assertIsNone(non_hmm.hmm_final_model_sha256)

    def test_validation_predicts_without_fitting_and_cannot_reselect(self):
        _, _, _, pair, _ = development_fixture()
        days = tuple(synthetic_day(i, "validation") for i in range(10, 16))
        before = canonical_json(pair)
        with patch.object(evaluation, "fit_standardizer", side_effect=AssertionError("validation refit")), \
                patch.object(evaluation, "fit_weighted_ols", side_effect=AssertionError("validation refit")), \
                patch.object(evaluation, "fit_weighted_logistic", side_effect=AssertionError("validation refit")):
            decision = evaluation.evaluate_validation(pair, days)
        self.assertEqual(decision.status, "CONFIRMED")
        self.assertGreater(decision.median_delta, 0)
        self.assertEqual(canonical_json(pair), before)
        self.assertEqual(evaluation.evaluate_validation(pair, days[:-1]).status, "COVERAGE_LIMITED")
        with self.assertRaisesRegex(ValueError, "mixes family/algorithm/config"):
            evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation", "other") for i in range(10, 16)))

    def test_validation_veto_does_not_unlock_test(self):
        _, _, _, pair, development = development_fixture()
        validation_days = []
        for index in range(10, 16):
            day = synthetic_day(index, "validation")
            predictions = pair.baseline_model.predict(tuple(pair.baseline_preprocessing.active_row(r.baseline_features)
                                                            for r in day.observations))
            rows = tuple(replace(row, outcome=prediction) for row, prediction in zip(day.observations, predictions))
            validation_days.append(replace(day, observations=rows))
        decision = evaluation.evaluate_validation(pair, tuple(validation_days))
        self.assertEqual(decision.status, "NOT_CONFIRMED")
        self.assertLess(decision.median_delta, 0)
        freeze = evaluation.freeze_validation(development, (decision,))
        with self.assertRaisesRegex(ValueError, "validation-confirmed"):
            evaluation.authorize_test(development, freeze, IDENTITY.family_id)
        negative_development = replace(pair, development_median=-pair.development_median)
        positive_validation = tuple(synthetic_day(i, "validation") for i in range(10, 16))
        self.assertEqual(evaluation.evaluate_validation(negative_development, positive_validation).status, "NOT_CONFIRMED")

    def test_test_requires_exact_hash_linked_authorization_and_never_refits(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)))
        validation = evaluation.freeze_validation(development, (decision,))
        authorization = evaluation.authorize_test(development, validation, IDENTITY.family_id)
        test_days = tuple(synthetic_day(i, "test") for i in range(18, 27))
        with patch.object(evaluation, "fit_standardizer", side_effect=AssertionError("test refit")), \
                patch.object(evaluation, "fit_weighted_ols", side_effect=AssertionError("test refit")):
            result = evaluation.evaluate_test(development, validation, authorization, test_days)
        self.assertEqual(result.status, "EVALUABLE")
        self.assertEqual(result.bootstrap.day_count, 9)
        self.assertEqual(result.bootstrap.day_effects, tuple(d.delta for d in result.day_results))
        self.assertEqual(result.classification, evaluation.EvidenceClassification.ROBUST_INCREMENTAL_EVIDENCE)
        unavailable = evaluation.evaluate_test(development, validation, authorization, test_days[:-1])
        self.assertEqual(unavailable.status, "COVERAGE_LIMITED")
        self.assertIsNone(unavailable.bootstrap)
        self.assertEqual(unavailable.day_results, ())
        for bad in (True, replace(authorization, development_freeze_sha256="b" * 64),
                    replace(authorization, identity=replace(IDENTITY, config_version="other")),
                    replace(authorization, validation_decision_sha256="b" * 64)):
            with self.assertRaises(ValueError):
                evaluation.evaluate_test(development, validation, bad, test_days)
        with self.assertRaises(ValueError):
            replace(authorization, horizon_minutes=60)
        with self.assertRaises(ValueError):
            replace(authorization, authorized=False)
        with self.assertRaises(ValueError):
            evaluation.freeze_validation(replace(development, study_manifest_sha256="b" * 64),
                                         (replace(decision, predictive_pair_sha256="c" * 64),))

    def test_scientific_hashes_bind_parameters_parent_freezes_and_hmm_placeholders(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)))
        validation = evaluation.freeze_validation(development, (decision,))
        authorization = evaluation.authorize_test(development, validation, IDENTITY.family_id)
        changed_model = replace(pair.extended_model, coefficients=(pair.extended_model.coefficients[0] + 0.1,)
                                + pair.extended_model.coefficients[1:])
        changed_pair = replace(pair, extended_model=changed_model)
        self.assertNotEqual(pair.pair_sha256, changed_pair.pair_sha256)
        parameter_development = replace(development, predictive_pairs=(changed_pair,))
        parameter_decision = evaluation.evaluate_validation(
            changed_pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)))
        parameter_validation = evaluation.freeze_validation(parameter_development, (parameter_decision,))
        parameter_authorization = evaluation.authorize_test(parameter_development, parameter_validation, IDENTITY.family_id)
        self.assertNotEqual(development.freeze_sha256, parameter_development.freeze_sha256)
        self.assertNotEqual(validation.freeze_sha256, parameter_validation.freeze_sha256)
        self.assertNotEqual(authorization.authorization_sha256, parameter_authorization.authorization_sha256)
        changed_development = replace(development, hmm_cross_fit_sha256="b" * 64,
                                      hmm_final_model_sha256="c" * 64)
        self.assertNotEqual(development.freeze_sha256, changed_development.freeze_sha256)
        changed_validation = evaluation.freeze_validation(changed_development, (decision,))
        self.assertNotEqual(validation.freeze_sha256, changed_validation.freeze_sha256)
        changed_authorization = evaluation.authorize_test(changed_development, changed_validation, IDENTITY.family_id)
        self.assertNotEqual(authorization.authorization_sha256, changed_authorization.authorization_sha256)
        with self.assertRaisesRegex(ValueError, "parent SHA mismatch"):
            evaluation.authorize_test(changed_development, validation, IDENTITY.family_id)
        self.assertEqual(json.loads(canonical_json(development))["study_manifest_sha256"], "a" * 64)
        self.assertIsInstance(hash(development), int)
        self.assertIsInstance(hash(validation), int)
        self.assertIsInstance(hash(authorization), int)


if __name__ == "__main__":
    unittest.main()
