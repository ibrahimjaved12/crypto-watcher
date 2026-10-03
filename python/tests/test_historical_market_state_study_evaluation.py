"""Synthetic aligned days only; never reads real study periods or artifacts."""

from copy import copy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import unittest
from unittest.mock import patch

from market_analysis import historical_market_state_study_evaluation as evaluation
from market_analysis.historical_market_state_study_features import (
    AlignedStudyDay, AlignedStudyObservation, BASELINE_FEATURES,
    FROZEN_EVALUATION_PLAN, canonical_json, predictive_family, FROZEN_PERIOD_ROSTER,
    FROZEN_STUDY_MANIFEST_SHA256, FinalizedPeriodProvenance, UpstreamInputProvenance,
    PRIMARY_CONFIRMATORY_FAMILY, EvidenceValueDay, scientific_sha256,
    layer_one_stratifier,
)


IDENTITY = evaluation.CandidateConfigIdentity("EXP-75-03", "synthetic-kalman-v1", "config-a")
HMM_CROSS_FIT_SHA256 = "b" * 64
HMM_FINAL_MODEL_SHA256 = "c" * 64


def source_period(index):
    _, day_date, phase = FROZEN_PERIOD_ROSTER.periods[index]
    return FinalizedPeriodProvenance(
        index, day_date, phase, FROZEN_STUDY_MANIFEST_SHA256, "d" * 64,
        "historical-market-state-study-period-report-v2", "synthetic-part-b-revision",
        scientific_sha256(("synthetic-period-report", index)),
        "historical-market-state-event-time-v1-context-v1", scientific_sha256(("event-v1", index)),
        "historical-market-state-bocpd-onset-evidence-v1", scientific_sha256(("bocpd", index)))


def phase_provenance(phase, cross_fit=HMM_CROSS_FIT_SHA256, final_model=HMM_FINAL_MODEL_SHA256):
    return UpstreamInputProvenance(phase, tuple(source_period(i) for i, _, assigned in
                                               FROZEN_PERIOD_ROSTER.periods if assigned == phase),
                                   cross_fit, final_model)


def development_provenance(cross_fit=HMM_CROSS_FIT_SHA256, final_model=HMM_FINAL_MODEL_SHA256):
    return phase_provenance("development", cross_fit, final_model)


def validation_provenance(cross_fit=HMM_CROSS_FIT_SHA256, final_model=HMM_FINAL_MODEL_SHA256):
    return phase_provenance("validation", cross_fit, final_model)


def test_provenance(cross_fit=HMM_CROSS_FIT_SHA256, final_model=HMM_FINAL_MODEL_SHA256):
    return phase_provenance("test", cross_fit, final_model)


def layer_one_fixture(nomination, common):
    spec = layer_one_stratifier(nomination.family_id)
    if nomination.status != "NOMINATED" or spec.mode != "CONTINUOUS_TERCILE":
        return None
    sample = next(sample for sample in common.samples if sample.identity == nomination.identity)
    column = next(i for i, f in enumerate(predictive_family(spec.family_id).candidate_features)
                  if f.name == spec.candidate_feature_name)
    return evaluation.LayerOneContinuousEvidence(
        nomination.identity, spec, tuple(EvidenceValueDay(
            day.study_period_index, day.utc_date, "development",
            tuple(row.candidate_features[column] for row in day.observations)) for day in sample.days),
        tuple((day.study_period_index, day.source_provenance.provenance_sha256) for day in sample.days))


@lru_cache(maxsize=15)
def unavailable_development_family(family):
    identity = evaluation.CandidateConfigIdentity(family, "synthetic-" + family + "-v1", "config-a")
    return evaluation.evaluate_development_family(fixed_identities(identity), ())


def development_freeze_fixture(manifest, results, nominations, pairs, common_samples, provenance, **kwargs):
    # Whole-study fixtures explicitly evaluate coverage for every remaining
    # registry config. Lower-level fixtures still exercise individual families.
    results, nominations, common_samples = list(results), list(nominations), list(common_samples)
    represented = {n.family_id for n in nominations}
    for family in PRIMARY_CONFIRMATORY_FAMILY:
        if family not in represented:
            common, family_results, nomination = unavailable_development_family(family)
            results.extend(family_results)
            nominations.append(nomination)
            common_samples.append(common)
    by_family = {common.samples[0].identity.family_id: common for common in common_samples}
    evidence = tuple(item for nomination in nominations
                     if (item := layer_one_fixture(nomination, by_family[nomination.family_id])) is not None)
    kwargs.setdefault("hmm_cross_fit_sha256", provenance.hmm_cross_fit_sha256)
    kwargs.setdefault("hmm_final_model_sha256", provenance.hmm_final_model_sha256)
    return evaluation.DevelopmentFreeze(manifest, tuple(results), tuple(nominations), pairs,
                                        tuple(common_samples), provenance, layer_one_evidence=evidence,
                                        layer_one_terciles=tuple(evaluation.freeze_layer_one_bins(e) for e in evidence),
                                        **kwargs)


def family_results(development, family=IDENTITY.family_id):
    return tuple(r for r in development.config_results if r.identity.family_id == family)


def family_common(development, family=IDENTITY.family_id):
    return next(c for c in development.common_samples if c.samples[0].identity.family_id == family)


def family_freeze_changes(development, results, nomination=None, pair=None):
    changes = {"config_results": tuple(r for r in development.config_results
                                       if r.identity.family_id != IDENTITY.family_id) + tuple(results)}
    if nomination is not None:
        changes["nominations"] = tuple(n for n in development.nominations
                                       if n.family_id != IDENTITY.family_id) + (nomination,)
    if pair is not None:
        changes["predictive_pairs"] = tuple(p for p in development.predictive_pairs
                                            if p.identity.family_id != IDENTITY.family_id) + (pair,)
    return changes


def synthetic_day(index, phase="development", config="config-a", count=32):
    """Independent basis columns give both designs full rank without randomness."""
    day_date = FROZEN_PERIOD_ROSTER.periods[index][1]
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
                           config, "CONTINUOUS", 15, "signed_market_return", "CONTINUOUS", count, tuple(rows), source_period(index))


def synthetic_event_day(index, config, empty=False):
    source = synthetic_day(index, config=config)
    spec = predictive_family("EXP-75-04B")
    row = source.observations[0]
    event = replace(row, family_id=spec.family_id, algorithm_version="synthetic-bocpd-v1",
                    observation_mode="EVENT", horizon_minutes=60, outcome_id="realized_volatility",
                    candidate_features=(0.1,), observation_key="event-" + config,
                    decision_time_ms=row.decision_time_ms + {"a": 5_000, "b": 10_000, "c": 15_000}[config])
    return AlignedStudyDay(index, source.utc_date, "development", spec.family_id, event.algorithm_version,
                           config, "EVENT", 60, "realized_volatility", "CONTINUOUS", None,
                           () if empty else (event,), source_period(index))


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
    freeze = development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (pair,), (common,), development_provenance())
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
        development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (), (event_common,), development_provenance())
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
        freeze = development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (), (common,), development_provenance())
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
                                         15, "v1_direction_persistence", "BINARY", 64, rows, source_period(index)))
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
        results = family_results(development)
        common = family_common(development)
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
                    replace(development, **family_freeze_changes(development, config_results))

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
                                        5, "signed_market_return", "CONTINUOUS", 32, rows, source_period(index)))
        common, results, nomination = evaluation.evaluate_development_family((identity,), tuple(days))
        self.assertEqual(results[0].status, "EVALUABLE")
        self.assertEqual(nomination.identity, identity)
        self.assertEqual(nomination.common_samples_sha256, common.samples_sha256)
        self.assertEqual(results[0].common_samples_sha256, common.samples_sha256)
        pair = evaluation.fit_final_development_pair(nomination, common.samples[0].days)
        freeze = development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (pair,), (common,), development_provenance())
        self.assertEqual(freeze.hmm_cross_fit_sha256, HMM_CROSS_FIT_SHA256)
        self.assertEqual(freeze.hmm_final_model_sha256, HMM_FINAL_MODEL_SHA256)
        with self.assertRaises(ValueError):
            evaluation.common_development_samples((identity, replace(identity, config_version="extra")), ())

    def test_different_common_sample_shas_cannot_nominate_or_freeze_together(self):
        _, _, _, _, development = development_fixture()
        results = family_results(development)
        different = replace(results[1], common_samples_sha256="b" * 64)
        mixed = (results[0], different, results[2])
        with self.assertRaisesRegex(ValueError, "different family common-sample SHAs"):
            evaluation.nominate_configuration(mixed)
        with self.assertRaisesRegex(ValueError, "different family common-sample SHAs"):
            replace(development, **family_freeze_changes(development, mixed))
        # A uniform arbitrary SHA is also insufficient: freeze must bind the
        # actual common-sample family object, not just matching result strings.
        arbitrary = tuple(replace(result, common_samples_sha256="b" * 64) for result in results)
        nomination = evaluation.nominate_configuration(arbitrary)
        pair = replace(development.predictive_pairs[0], nomination_sha256=nomination.nomination_sha256,
                       common_samples_sha256=nomination.common_samples_sha256)
        with self.assertRaisesRegex(ValueError, "common-sample identity"):
            replace(development, **family_freeze_changes(development, arbitrary, nomination, pair))
        with self.assertRaises(ValueError):
            replace(development, common_samples=())

    def test_freeze_checks_per_config_rows_against_the_common_family_object(self):
        _, _, _, _, development = development_fixture()
        results = tuple(replace(result, development_sample_sha256="b" * 64)
                        for result in family_results(development))
        nomination = evaluation.nominate_configuration(results)
        pair = replace(development.predictive_pairs[0], nomination_sha256=nomination.nomination_sha256,
                       development_sample_sha256=nomination.development_sample_sha256)
        with self.assertRaisesRegex(ValueError, "common-sample rows/coverage"):
            replace(development, **family_freeze_changes(development, results, nomination, pair))

    def test_hmm_identities_are_required_at_development_freeze(self):
        identity = evaluation.CandidateConfigIdentity("EXP-75-09", "synthetic-hmm-v1", "only-config")
        common, results, nomination = evaluation.evaluate_development_family((identity,), ())
        for cross_fit, final_model in ((None, None), ("b" * 64, None), (None, "c" * 64),
                                       ("invalid", "c" * 64)):
            with self.subTest(cross_fit=cross_fit, final_model=final_model), self.assertRaises(ValueError):
                development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (), (common,), development_provenance(cross_fit, final_model),
                                             hmm_cross_fit_sha256=cross_fit, hmm_final_model_sha256=final_model)
        freeze = development_freeze_fixture(FROZEN_STUDY_MANIFEST_SHA256, results, (nomination,), (), (common,), development_provenance("b" * 64, "c" * 64),
                                               hmm_cross_fit_sha256="b" * 64, hmm_final_model_sha256="c" * 64)
        self.assertEqual(freeze.hmm_cross_fit_sha256, "b" * 64)
        self.assertEqual(freeze.hmm_final_model_sha256, "c" * 64)
        complete = development_fixture()[-1]
        self.assertEqual(complete.hmm_cross_fit_sha256, HMM_CROSS_FIT_SHA256)
        self.assertEqual(complete.hmm_final_model_sha256, HMM_FINAL_MODEL_SHA256)

    def test_validation_predicts_without_fitting_and_cannot_reselect(self):
        _, _, _, pair, _ = development_fixture()
        days = tuple(synthetic_day(i, "validation") for i in range(10, 16))
        before = canonical_json(pair)
        with patch.object(evaluation, "fit_standardizer", side_effect=AssertionError("validation refit")), \
                patch.object(evaluation, "fit_weighted_ols", side_effect=AssertionError("validation refit")), \
                patch.object(evaluation, "fit_weighted_logistic", side_effect=AssertionError("validation refit")):
            decision = evaluation.evaluate_validation(pair, days, validation_provenance(), development_fixture()[-1])
        self.assertEqual(decision.status, "CONFIRMED")
        self.assertGreater(decision.median_delta, 0)
        self.assertEqual(canonical_json(pair), before)
        self.assertEqual(evaluation.evaluate_validation(pair, days[:-1], validation_provenance(), development_fixture()[-1]).status, "COVERAGE_LIMITED")
        with self.assertRaisesRegex(ValueError, "mixes family/algorithm/config"):
            evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation", "other") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])

    def test_validation_veto_does_not_unlock_test(self):
        _, _, _, pair, development = development_fixture()
        validation_days = []
        for index in range(10, 16):
            day = synthetic_day(index, "validation")
            predictions = pair.baseline_model.predict(tuple(pair.baseline_preprocessing.active_row(r.baseline_features)
                                                            for r in day.observations))
            rows = tuple(replace(row, outcome=prediction) for row, prediction in zip(day.observations, predictions))
            validation_days.append(replace(day, observations=rows))
        decision = evaluation.evaluate_validation(pair, tuple(validation_days), validation_provenance(), development_fixture()[-1])
        self.assertEqual(decision.status, "NOT_CONFIRMED")
        self.assertLess(decision.median_delta, 0)
        freeze = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, freeze)
        self.assertEqual(next(m for m in authorization.members if m.family_id == IDENTITY.family_id).status, "VETOED")
        with self.assertRaisesRegex(ValueError, "validation-confirmed"):
            evaluation.evaluate_test(development, freeze, authorization, IDENTITY.family_id, (), test_provenance())
        negative_development = replace(pair, development_median=-pair.development_median)
        positive_validation = tuple(synthetic_day(i, "validation") for i in range(10, 16))
        self.assertEqual(evaluation.evaluate_validation(negative_development, positive_validation, validation_provenance(), development_fixture()[-1]).status, "NOT_CONFIRMED")

    def test_test_requires_exact_hash_linked_authorization_and_never_refits(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        validation = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        test_days = tuple(synthetic_day(i, "test") for i in range(18, 27))
        with patch.object(evaluation, "fit_standardizer", side_effect=AssertionError("test refit")), \
                patch.object(evaluation, "fit_weighted_ols", side_effect=AssertionError("test refit")):
            result = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, test_days, test_provenance())
        self.assertEqual(result.status, "EVALUABLE")
        self.assertEqual(result.bootstrap.day_count, 9)
        self.assertEqual(result.bootstrap.day_effects, tuple(d.delta for d in result.day_results))
        self.assertEqual(result.classification, evaluation.EvidenceClassification.ROBUST_INCREMENTAL_EVIDENCE)
        unavailable = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, test_days[:-1], test_provenance())
        self.assertEqual(unavailable.status, "COVERAGE_LIMITED")
        self.assertIsNone(unavailable.bootstrap)
        self.assertEqual(unavailable.day_results, ())
        member = next(m for m in authorization.members if m.family_id == IDENTITY.family_id)
        def changed_member(**changes):
            return replace(authorization, members=tuple(replace(m, **changes) if m == member else m
                                                        for m in authorization.members))
        for bad in (True, replace(authorization, development_freeze_sha256="b" * 64),
                    changed_member(identity=replace(IDENTITY, config_version="other")),
                    changed_member(validation_decision_sha256="b" * 64)):
            with self.assertRaises(ValueError):
                evaluation.evaluate_test(development, validation, bad, IDENTITY.family_id, test_days, test_provenance())
        with self.assertRaises(ValueError):
            replace(development, study_manifest_sha256="b" * 64)
        with self.assertRaises(ValueError):
            evaluation.freeze_validation(development, (replace(decision, predictive_pair_sha256="c" * 64),), validation_provenance())

    def test_scientific_hashes_bind_parameters_parent_freezes_and_hmm_placeholders(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        validation = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        changed_model = replace(pair.extended_model, coefficients=(pair.extended_model.coefficients[0] + 0.1,)
                                + pair.extended_model.coefficients[1:])
        changed_pair = replace(pair, extended_model=changed_model)
        self.assertNotEqual(pair.pair_sha256, changed_pair.pair_sha256)
        parameter_development = replace(development, predictive_pairs=(changed_pair,))
        parameter_decision = evaluation.evaluate_validation(
            changed_pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        parameter_validation = evaluation.freeze_validation(parameter_development, (parameter_decision,), validation_provenance())
        parameter_authorization = evaluation.authorize_test(parameter_development, parameter_validation)
        self.assertNotEqual(development.freeze_sha256, parameter_development.freeze_sha256)
        self.assertNotEqual(validation.freeze_sha256, parameter_validation.freeze_sha256)
        self.assertNotEqual(authorization.authorization_sha256, parameter_authorization.authorization_sha256)
        changed_development = replace(development, hmm_cross_fit_sha256="d" * 64,
                                      hmm_final_model_sha256="e" * 64,
                                      upstream_provenance=development_provenance("d" * 64, "e" * 64))
        self.assertNotEqual(development.freeze_sha256, changed_development.freeze_sha256)
        changed_decision = replace(decision, upstream_provenance_sha256=validation_provenance("d" * 64, "e" * 64).provenance_sha256)
        changed_validation = evaluation.freeze_validation(changed_development, (changed_decision,), validation_provenance("d" * 64, "e" * 64))
        self.assertNotEqual(validation.freeze_sha256, changed_validation.freeze_sha256)
        changed_authorization = evaluation.authorize_test(changed_development, changed_validation)
        self.assertNotEqual(authorization.authorization_sha256, changed_authorization.authorization_sha256)
        with self.assertRaisesRegex(ValueError, "parent SHA mismatch"):
            evaluation.authorize_test(changed_development, validation)
        self.assertEqual(json.loads(canonical_json(development))["study_manifest_sha256"], FROZEN_STUDY_MANIFEST_SHA256)
        self.assertIsInstance(hash(development), int)
        self.assertIsInstance(hash(validation), int)
        self.assertIsInstance(hash(authorization), int)

    def test_roster_identity_and_missing_days_are_explicit(self):
        day = synthetic_day(0)
        for changes in ({"study_period_index": 1}, {"utc_date": day.utc_date + timedelta(days=1)},
                        {"phase": "test"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(day, **changes)
        for phase, count in (("development", 10), ("validation", 8), ("test", 12)):
            coverage = evaluation.phase_coverage((), phase)
            self.assertEqual(len(coverage.days), count)
            self.assertEqual(coverage.supplied_day_count, 0)
            self.assertTrue(all(d.status == "COVERAGE_LIMITED" and d.reason == "MISSING_FROZEN_DAY"
                                for d in coverage.days))
        coverage = evaluation.phase_coverage((day,), "development")
        self.assertEqual(tuple(d.study_period_index for d in coverage.days), tuple(range(10)))
        self.assertEqual(coverage.days[1].utc_date, FROZEN_PERIOD_ROSTER.periods[1][1])
        with self.assertRaises(ValueError):
            replace(coverage, days=coverage.days[:1])

    def test_upstream_mixed_scientific_provenance_fails_closed(self):
        provenance = development_provenance()
        first, second = provenance.periods[:2]
        for field, value in (("extension_coverage_manifest_sha256", "e" * 64),
                             ("part_b_code_revision", "different-revision")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "mixed Part-B"):
                replace(provenance, periods=(first, replace(second, **{field: value})) + provenance.periods[2:])
        for changes in ({"part_b_report_schema_version": "different-schema"},
                        {"study_manifest_sha256": "e" * 64},
                        {"event_time_v1_context_version": "different-context"},
                        {"bocpd_onset_evidence_version": "different-onset"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(second, **changes)
        for field in ("hmm_cross_fit_sha256", "hmm_final_model_sha256"):
            mismatched = replace(first, **{field: "e" * 64})
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "mixed HMM"):
                replace(provenance, periods=(mismatched,) + provenance.periods[1:],
                        hmm_cross_fit_sha256="b" * 64, hmm_final_model_sha256="c" * 64)
        for periods in ((second, first) + provenance.periods[2:], (first, first) + provenance.periods[2:]):
            with self.assertRaises(ValueError):
                replace(provenance, periods=periods)
        days = (synthetic_day(0), replace(synthetic_day(1), source_provenance=replace(
            second, extension_coverage_manifest_sha256="e" * 64)))
        with self.assertRaises(ValueError):
            evaluation.evaluate_development_family(fixed_identities(), family_days(IDENTITY, days))

    def test_source_report_and_event_identities_are_bound_to_freeze(self):
        development = development_fixture()[-1]
        upstream = development.upstream_provenance
        for field in ("period_report_sha256", "event_time_v1_context_sha256", "bocpd_onset_evidence_sha256"):
            altered = replace(upstream.periods[0], **{field: "e" * 64})
            changed = replace(upstream, periods=(altered,) + upstream.periods[1:])
            self.assertNotEqual(changed.provenance_sha256, upstream.provenance_sha256)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "upstream period-report"):
                replace(development, upstream_provenance=changed)
        self.assertEqual(development.upstream_provenance_sha256, upstream.provenance_sha256)
        pair = development.predictive_pairs[0]
        day = synthetic_day(10, "validation")
        altered = replace(day.source_provenance, period_report_sha256="e" * 64)
        validation_days = (replace(day, source_provenance=altered),) + tuple(
            synthetic_day(i, "validation") for i in range(11, 16))
        with patch.object(evaluation, "_score_day", side_effect=AssertionError("scored")):
            with self.assertRaisesRegex(ValueError, "upstream period-report"):
                evaluation.evaluate_validation(pair, validation_days, validation_provenance(), development_fixture()[-1])

    def test_test_rejects_mismatched_upstream_before_scoring(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        validation = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        day = synthetic_day(18, "test")
        for field in ("period_report_sha256", "event_time_v1_context_sha256", "bocpd_onset_evidence_sha256"):
            altered = replace(day, source_provenance=replace(day.source_provenance, **{field: "e" * 64}))
            with self.subTest(field=field), patch.object(evaluation, "_score_day", side_effect=AssertionError("scored")):
                with self.assertRaisesRegex(ValueError, "upstream period-report"):
                    evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (altered,), test_provenance())
        with self.assertRaises(ValueError):
            replace(development, hmm_cross_fit_sha256="e" * 64)

    def test_authorization_retains_coverage_limited_and_fit_failed_members(self):
        _, _, _, pair, development = development_fixture()
        limited = evaluation.evaluate_validation(pair, (), validation_provenance(), development_fixture()[-1])
        freeze = evaluation.freeze_validation(development, (limited,), validation_provenance())
        authorization = evaluation.authorize_test(development, freeze)
        member = next(m for m in authorization.members if m.family_id == IDENTITY.family_id)
        self.assertEqual(member.status, "COVERAGE_LIMITED")
        self.assertEqual(member.identity, IDENTITY)
        self.assertEqual(member.predictive_pair_sha256, pair.pair_sha256)
        self.assertEqual(member.validation_decision_sha256, limited.decision_sha256)
        with self.assertRaises(ValueError):
            evaluation.evaluate_test(development, freeze, authorization, IDENTITY.family_id, (), test_provenance())
        adequate = evaluation.phase_coverage(tuple(synthetic_day(i, "validation") for i in range(10, 16)), "validation")
        failed = evaluation.ValidationDecision(IDENTITY, validation_provenance().provenance_sha256,
                                               pair.pair_sha256, pair.development_median,
                                               adequate, (), "FIT_FAILED", "SYNTHETIC_FAILURE")
        freeze = evaluation.freeze_validation(development, (failed,), validation_provenance())
        authorization = evaluation.authorize_test(development, freeze)
        member = next(m for m in authorization.members if m.family_id == IDENTITY.family_id)
        self.assertEqual(member.status, "NOT_EVALUABLE")
        self.assertEqual(member.validation_decision_sha256, failed.decision_sha256)
        self.assertEqual(len(authorization.members), 15)

    def test_aggregate_authorization_is_complete_immutable_and_cannot_be_extended(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        validation = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        self.assertEqual(tuple(m.family_id for m in authorization.members), PRIMARY_CONFIRMATORY_FAMILY)
        self.assertEqual(len(authorization.members), 15)
        with self.assertRaises(FrozenInstanceError):
            authorization.members = authorization.members[:-1]
        self.assertNotIn("EXP-75-04A", PRIMARY_CONFIRMATORY_FAMILY)
        unavailable = next(m for m in authorization.members if m.family_id == "EXP-75-01")
        self.assertEqual(unavailable.status, "NOT_EVALUABLE")
        with self.assertRaises(ValueError):
            evaluation.evaluate_test(development, validation, authorization, unavailable.family_id, (), test_provenance())
        with self.assertRaises(ValueError):
            replace(authorization, members=authorization.members[:-1])
        with self.assertRaises(ValueError):
            replace(authorization, members=authorization.members + (unavailable,))
        # Changing an unavailable member requires a different complete freeze;
        # it still cannot authorize test against the existing parent freezes.
        changed = replace(authorization, members=tuple(
            replace(m, status="COVERAGE_LIMITED") if m == unavailable else m for m in authorization.members))
        self.assertNotEqual(changed.authorization_sha256, authorization.authorization_sha256)
        with self.assertRaises(ValueError):
            evaluation.evaluate_test(development, validation, changed, IDENTITY.family_id, (), test_provenance())
        with self.assertRaises(TypeError):
            evaluation.authorize_test(development, validation, IDENTITY.family_id)

    def test_study_holm_retains_all_15_with_effective_one_for_unavailable(self):
        _, _, _, pair, development = development_fixture()
        decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)), validation_provenance(), development_fixture()[-1])
        validation = evaluation.freeze_validation(development, (decision,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        result = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id,
                                          tuple(synthetic_day(i, "test") for i in range(18, 27)), test_provenance())
        members = evaluation.study_primary_holm(authorization, (result,))
        self.assertEqual(tuple(m.hypothesis_id for m in members), PRIMARY_CONFIRMATORY_FAMILY)
        selected = next(m for m in members if m.hypothesis_id == IDENTITY.family_id)
        self.assertEqual(selected.adjusted_p, min(1, 15 * result.sign_test.raw_p))
        self.assertTrue(all(m.raw_p == 1 and m.status == "NOT_TESTABLE" for m in members
                            if m.hypothesis_id != IDENTITY.family_id))
        missing = evaluation.study_primary_holm(authorization, ())
        self.assertEqual(next(m for m in missing if m.hypothesis_id == IDENTITY.family_id).status, "MISSING")
        self.assertTrue(all(m.raw_p == 1 for m in missing))
        veto = replace(decision, day_results=tuple(replace(
            r, baseline_loss=1, extended_loss=2, delta=-1) for r in decision.day_results), status="NOT_CONFIRMED")
        veto_validation = evaluation.freeze_validation(development, (veto,), validation_provenance())
        veto_authorization = evaluation.authorize_test(development, veto_validation)
        veto_member = next(m for m in evaluation.study_primary_holm(veto_authorization, ())
                           if m.hypothesis_id == IDENTITY.family_id)
        self.assertEqual((veto_member.status, veto_member.raw_p), ("VETOED", 1))
        failed = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (), test_provenance())
        failed_member = next(m for m in evaluation.study_primary_holm(authorization, (failed,))
                             if m.hypothesis_id == IDENTITY.family_id)
        self.assertEqual((failed_member.status, failed_member.raw_p), ("NOT_TESTABLE", 1))

    def test_layer_one_bins_bind_selected_registry_spec_and_source(self):
        days, _, _, _, development = development_fixture()
        evidence = development.layer_one_evidence[0]
        binding = development.layer_one_terciles[0]
        spec = layer_one_stratifier(IDENTITY.family_id)
        self.assertEqual(evidence.stratifier, spec)
        for changes in ({"identity": replace(IDENTITY, family_id="EXP-75-07")},
                        {"identity": replace(IDENTITY, config_version="config-b")},
                        {"stratifier": "arbitrary-label"},
                        {"stratifier": replace(spec, source_identity="other", candidate_feature_name="other")},
                        {"stratifier": replace(spec, mode="NATIVE_CATEGORY", candidate_feature_name=None,
                                               allowed_categories=("UP",))},
                        {"development_evidence_sha256": "e" * 64}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "Layer-1"):
                replace(development, layer_one_terciles=(replace(binding, **changes),))
        with self.assertRaises(ValueError):
            replace(evidence, stratifier="arbitrary-label")
        with self.assertRaises(ValueError):
            replace(development, layer_one_terciles=(), layer_one_evidence=())
        with self.assertRaises(ValueError):
            replace(development, layer_one_terciles=(binding, binding))
        with self.assertRaises(ValueError):
            replace(development, layer_one_terciles=(("arbitrary-label", binding.bins),))
        altered_evidence = replace(evidence, source_period_provenance=((0, "e" * 64),)
                                   + evidence.source_period_provenance[1:])
        with self.assertRaises(ValueError):
            replace(development, layer_one_terciles=(evaluation.freeze_layer_one_bins(altered_evidence),),
                    layer_one_evidence=(altered_evidence,))
        changed_day = replace(evidence.days[0], values=(99.0,) * len(evidence.days[0].values))
        changed_evidence = replace(evidence, days=(changed_day,) + evidence.days[1:])
        with self.assertRaises(ValueError):
            replace(development, layer_one_evidence=(changed_evidence,))
        changed_freeze = replace(development, layer_one_evidence=(changed_evidence,),
                                layer_one_terciles=(evaluation.freeze_layer_one_bins(changed_evidence),))
        self.assertNotEqual(changed_freeze.freeze_sha256, development.freeze_sha256)
        for family in ("EXP-75-02", "EXP-75-04B", "EXP-75-09"):
            native = layer_one_stratifier(family)
            self.assertEqual(native.mode, "NATIVE_CATEGORY")
            with self.assertRaises(ValueError):
                replace(evidence, identity=replace(IDENTITY, family_id=family), stratifier=native)

    def test_whole_study_freeze_requires_every_family_nomination_and_config(self):
        development = development_fixture()[-1]
        expected = set(PRIMARY_CONFIRMATORY_FAMILY)
        self.assertEqual({n.family_id for n in development.nominations}, expected)
        self.assertEqual({c.samples[0].identity.family_id for c in development.common_samples}, expected)
        for family in PRIMARY_CONFIRMATORY_FAMILY:
            results = family_results(development, family)
            self.assertEqual(len(results), predictive_family(family).expected_config_count)
            with self.subTest(family=family):
                with self.assertRaisesRegex(ValueError, "family membership"):
                    replace(development,
                            config_results=tuple(r for r in development.config_results if r.identity.family_id != family),
                            nominations=tuple(n for n in development.nominations if n.family_id != family),
                            common_samples=tuple(c for c in development.common_samples
                                                 if c.samples[0].identity.family_id != family),
                            predictive_pairs=tuple(p for p in development.predictive_pairs if p.identity.family_id != family),
                            layer_one_terciles=tuple(b for b in development.layer_one_terciles if b.identity.family_id != family),
                            layer_one_evidence=tuple(e for e in development.layer_one_evidence if e.identity.family_id != family))
                with self.assertRaisesRegex(ValueError, "family membership"):
                    replace(development, nominations=tuple(n for n in development.nominations if n.family_id != family))
                with self.assertRaises(ValueError):
                    replace(development, config_results=tuple(r for r in development.config_results if r != results[0]))
                with self.assertRaises(ValueError):
                    replace(development, common_samples=tuple(c for c in development.common_samples
                                                              if c.samples[0].identity.family_id != family))
        with self.assertRaises(ValueError):
            replace(development, hmm_cross_fit_sha256=None, hmm_final_model_sha256=None,
                    upstream_provenance=development_provenance(None, None),
                    config_results=tuple(r for r in development.config_results if r.identity.family_id != "EXP-75-09"),
                    nominations=tuple(n for n in development.nominations if n.family_id != "EXP-75-09"),
                    common_samples=tuple(c for c in development.common_samples
                                         if c.samples[0].identity.family_id != "EXP-75-09"))

    def test_unavailable_membership_is_explicit_and_cannot_be_manufactured(self):
        development = development_fixture()[-1]
        family = "EXP-75-01"
        results = family_results(development, family)
        nomination = next(n for n in development.nominations if n.family_id == family)
        self.assertTrue(all(r.status == "COVERAGE_LIMITED" for r in results))
        self.assertEqual(nomination, evaluation.nominate_configuration(results))
        self.assertEqual(nomination.status, "NOT_EVALUABLE")
        self.assertNotIn(family, {p.identity.family_id for p in development.predictive_pairs})
        limited = evaluation.evaluate_validation(development.predictive_pairs[0], (), validation_provenance(), development)
        validation = evaluation.freeze_validation(development, (limited,), validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        member = next(m for m in authorization.members if m.family_id == family)
        self.assertEqual(member.status, "NOT_EVALUABLE")
        self.assertEqual(member.nomination_sha256, nomination.nomination_sha256)
        self.assertIsNone(member.identity)
        self.assertIsNone(member.predictive_pair_sha256)
        self.assertIsNone(member.validation_decision_sha256)
        # Revalidation at authorization rejects even an in-memory object whose
        # constructor was bypassed; missing membership cannot acquire a status.
        missing = copy(development)
        object.__setattr__(missing, "nominations", tuple(n for n in development.nominations if n.family_id != family))
        with self.assertRaises(ValueError):
            evaluation.authorize_test(missing, validation)
        with self.assertRaises(ValueError):
            evaluation.freeze_validation(development, (), validation_provenance())

    def test_study_holm_rejects_two_valid_results_with_different_test_provenance(self):
        original = development_fixture()[-1]
        mark_identity = evaluation.CandidateConfigIdentity("EXP-75-10", "synthetic-mark-trade-v1", "only-config")
        def mark_day(index, phase="development"):
            source = synthetic_day(index, phase)
            rows = tuple(replace(row, family_id=mark_identity.family_id,
                                 algorithm_version=mark_identity.algorithm_version,
                                 config_version=mark_identity.config_version, horizon_minutes=5,
                                 candidate_features=(row.candidate_features[0],)) for row in source.observations)
            return replace(source, family_id=mark_identity.family_id,
                           algorithm_version=mark_identity.algorithm_version,
                           config_version=mark_identity.config_version, horizon_minutes=5, observations=rows)
        common, results, nomination = evaluation.evaluate_development_family(
            (mark_identity,), tuple(mark_day(i) for i in range(8)))
        mark_pair = evaluation.fit_final_development_pair(nomination, common.samples[0].days)
        kalman_nomination = next(n for n in original.nominations if n.family_id == IDENTITY.family_id)
        development = development_freeze_fixture(
            FROZEN_STUDY_MANIFEST_SHA256, family_results(original) + results,
            (kalman_nomination, nomination), original.predictive_pairs + (mark_pair,),
            (family_common(original), common), development_provenance())
        kalman_pair = next(p for p in development.predictive_pairs if p.identity == IDENTITY)
        decisions = (
            evaluation.evaluate_validation(kalman_pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)),
                                           validation_provenance(), development),
            evaluation.evaluate_validation(mark_pair, tuple(mark_day(i, "validation") for i in range(10, 16)),
                                           validation_provenance(), development))
        self.assertTrue(all(d.status == "CONFIRMED" for d in decisions))
        validation = evaluation.freeze_validation(development, decisions, validation_provenance())
        authorization = evaluation.authorize_test(development, validation)
        test_inputs = test_provenance()
        first = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id,
                                         tuple(synthetic_day(i, "test") for i in range(18, 27)), test_inputs)
        second = evaluation.evaluate_test(development, validation, authorization, mark_identity.family_id,
                                          tuple(mark_day(i, "test") for i in range(18, 27)), test_inputs)
        self.assertEqual(len(evaluation.study_primary_holm(authorization, (first, second))), 15)
        altered_inputs = replace(test_inputs, periods=(replace(test_inputs.periods[0], period_report_sha256="e" * 64),)
                                 + test_inputs.periods[1:])
        lookup = {p.study_period_index: p for p in altered_inputs.periods}
        altered = evaluation.evaluate_test(development, validation, authorization, mark_identity.family_id,
                                           tuple(replace(mark_day(i, "test"), source_provenance=lookup[i])
                                                 for i in range(18, 27)), altered_inputs)
        self.assertEqual(first.authorization, altered.authorization)
        self.assertNotEqual(first.upstream_provenance_sha256, altered.upstream_provenance_sha256)
        self.assertEqual(len(evaluation.study_primary_holm(authorization, (altered,))), 15)
        with self.assertRaisesRegex(ValueError, "same exact test provenance"):
            evaluation.study_primary_holm(authorization, (first, altered))
        with self.assertRaises(ValueError):
            evaluation.study_primary_holm(authorization, (first, first))

    def test_exact_phase_provenance_and_opening_order(self):
        for phase, count in (("development", 10), ("validation", 8), ("test", 12)):
            provenance = phase_provenance(phase)
            self.assertEqual(len(provenance.periods), count)
            self.assertEqual(tuple((p.study_period_index, p.utc_date, p.phase) for p in provenance.periods),
                             tuple(p for p in FROZEN_PERIOD_ROSTER.periods if p[2] == phase))
            for periods in (provenance.periods[:-1], tuple(reversed(provenance.periods)),
                            provenance.periods + (source_period(0 if phase != "development" else 10),)):
                with self.assertRaises(ValueError):
                    replace(provenance, periods=periods)
        development = development_fixture()[-1]
        pair = development.predictive_pairs[0]
        with self.assertRaises(ValueError):
            replace(development, upstream_provenance=validation_provenance())
        with patch.object(evaluation, "_score_day", side_effect=AssertionError("scored")):
            with self.assertRaises(ValueError):
                evaluation.evaluate_validation(pair, (), development_provenance(), development)
        # No test report identity is constructed until the aggregate gate exists.
        source_period_original = source_period
        def before_test_open(index):
            if index >= 18:
                raise AssertionError("test opened before authorization")
            return source_period_original(index)
        with patch(__name__ + ".source_period", side_effect=before_test_open):
            validation_inputs = validation_provenance()
            decision = evaluation.evaluate_validation(pair, tuple(synthetic_day(i, "validation") for i in range(10, 16)),
                                                       validation_inputs, development)
            validation = evaluation.freeze_validation(development, (decision,), validation_inputs)
            authorization = evaluation.authorize_test(development, validation)
        self.assertNotEqual(validation.upstream_provenance.provenance_sha256, development.upstream_provenance_sha256)
        self.assertEqual(authorization.shared_contract, development.upstream_provenance.shared_contract)
        test_inputs = test_provenance()
        result = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id,
                                          tuple(synthetic_day(i, "test") for i in range(18, 27)), test_inputs)
        self.assertEqual(result.upstream_provenance_sha256, test_inputs.provenance_sha256)
        with patch.object(evaluation, "_score_day", side_effect=AssertionError("scored")):
            for wrong in (development_provenance(), validation_inputs,
                          replace(test_inputs, periods=tuple(replace(p, part_b_code_revision="other")
                                                            for p in test_inputs.periods)),
                          replace(test_inputs, periods=tuple(replace(p, extension_coverage_manifest_sha256="e" * 64)
                                                            for p in test_inputs.periods)),
                          test_provenance("d" * 64, "e" * 64)):
                with self.assertRaises(ValueError):
                    evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (), wrong)
            with self.assertRaises(ValueError):
                mixed = replace(test_inputs, periods=(source_period(0),) + test_inputs.periods[1:])
                evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (), mixed)
        changed_report = replace(test_inputs, periods=(replace(test_inputs.periods[0], period_report_sha256="e" * 64),)
                                 + test_inputs.periods[1:])
        changed = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (), changed_report)
        missing = evaluation.evaluate_test(development, validation, authorization, IDENTITY.family_id, (), test_inputs)
        self.assertNotEqual(changed.result_sha256, missing.result_sha256)
        self.assertEqual(evaluation.authorize_test(development, validation), authorization)

    def test_shared_contract_changes_reject_validation_before_scoring(self):
        development = development_fixture()[-1]
        pair = development.predictive_pairs[0]
        provenance = validation_provenance()
        for name, value in (("part_b_code_revision", "other"), ("extension_coverage_manifest_sha256", "e" * 64)):
            changed = replace(provenance, periods=tuple(replace(p, **{name: value}) for p in provenance.periods))
            with patch.object(evaluation, "_score_day", side_effect=AssertionError("scored")):
                with self.assertRaisesRegex(ValueError, "shared Part-B"):
                    evaluation.evaluate_validation(pair, (), changed, development)
        with self.assertRaises(ValueError):
            evaluation.freeze_validation(development, (), development_provenance())
        decision = evaluation.evaluate_validation(pair, (), provenance, development)
        changed_reports = replace(provenance, periods=(replace(provenance.periods[0], period_report_sha256="e" * 64),)
                                  + provenance.periods[1:])
        with self.assertRaisesRegex(ValueError, "frozen upstream provenance"):
            evaluation.freeze_validation(development, (decision,), changed_reports)
        frozen = evaluation.freeze_validation(development, (decision,), provenance)
        with self.assertRaises(ValueError):
            evaluation.authorize_test(development, replace(frozen, upstream_provenance=changed_reports))


if __name__ == "__main__":
    unittest.main()
