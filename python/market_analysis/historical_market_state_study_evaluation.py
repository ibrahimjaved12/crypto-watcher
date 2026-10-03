"""Part-C evaluation over aligned domain objects, with hash-linked phase gates.

There is deliberately no file loader or CLI. A later adapter must verify Part-B
provenance, causal event independence and HMM cross-fit identities before
constructing these objects. This module never generates those representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from statistics import median

from .historical_market_state_study_features import (
    AlignedStudyDay, BASELINE_FEATURES, FROZEN_EVALUATION_PLAN, PHASE_DAY_COUNTS,
    PHASE_MINIMUM_DAYS, StudyEvaluationPlan, TercileSpec, finite_number,
    predictive_family, require_name, require_sha256, scientific_sha256, seal_hash,
)
from .historical_market_state_study_models import (
    FrozenLinearModel, FrozenLogisticModel, FrozenStandardizer, StudyModelFitError,
    day_balanced_weights, fit_standardizer, fit_weighted_logistic, fit_weighted_ols,
)
from .historical_market_state_study_statistics import (
    BootstrapNamespace, DayBootstrapResult, DaySignTestResult, EvidenceClassification,
    EvidenceComponents, classify_evidence, day_loss, exact_day_sign_test,
    incremental_effect, whole_day_bootstrap,
)


@dataclass(frozen=True)
class CandidateConfigIdentity:
    family_id: str
    algorithm_version: str
    config_version: str

    def __post_init__(self):
        predictive_family(self.family_id)
        require_name(self.algorithm_version)
        require_name(self.config_version)


def _day_identity(day):
    return CandidateConfigIdentity(day.family_id, day.algorithm_version, day.config_version)


def _fixed_family_identities(identities):
    identities = tuple(identities)
    if (not identities or any(not isinstance(i, CandidateConfigIdentity) for i in identities)
            or len({i.family_id for i in identities}) != 1
            or len({i.config_version for i in identities}) != len(identities)
            or len({i.algorithm_version for i in identities}) != 1):
        raise ValueError("fixed family requires unique configs and one family/algorithm identity")
    spec = predictive_family(identities[0].family_id)
    if len(identities) != spec.expected_config_count:
        raise ValueError("fixed config count differs from the frozen family cardinality")
    return spec


def _cohort(days, phase, identity=None):
    days = tuple(days)
    if phase not in dict(PHASE_DAY_COUNTS):
        raise ValueError("unknown study phase")
    if (any(not isinstance(day, AlignedStudyDay) or day.phase != phase for day in days)
            or len({d.study_period_index for d in days}) != len(days)
            or len({d.utc_date for d in days}) != len(days)
            or len(days) > dict(PHASE_DAY_COUNTS)[phase]):
        raise ValueError("cohort must contain unique days from exactly the requested phase")
    if identity is None and days:
        identity = _day_identity(days[0])
    if any(_day_identity(day) != identity for day in days):
        raise ValueError("cohort mixes family/algorithm/config identities")
    return tuple(sorted(days, key=lambda day: day.study_period_index))


@dataclass(frozen=True)
class DayEligibility:
    study_period_index: int
    utc_date: date
    usable_count: int
    required_count: int
    status: str
    reason: str

    def __post_init__(self):
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date
                or type(self.usable_count) is not int or self.usable_count < 0
                or type(self.required_count) is not int or self.required_count < 1
                or self.status != ("ELIGIBLE" if self.usable_count >= self.required_count else "COVERAGE_LIMITED")):
            raise ValueError("invalid day eligibility contract")


def day_eligibility(day: AlignedStudyDay) -> DayEligibility:
    # ceil(0.50*n), without floating-point rounding at odd scheduled counts.
    required = (day.scheduled_primary_count + 1) // 2 if day.observation_mode == "CONTINUOUS" else 1
    eligible = len(day.observations) >= required
    return DayEligibility(day.study_period_index, day.utc_date, len(day.observations), required,
                          "ELIGIBLE" if eligible else "COVERAGE_LIMITED",
                          "PRIMARY_ROWS_AVAILABLE" if eligible else "INSUFFICIENT_PRIMARY_ROWS")


@dataclass(frozen=True)
class PhaseCoverage:
    phase: str
    scheduled_day_count: int
    supplied_day_count: int
    eligible_day_count: int
    minimum_day_count: int
    days: tuple[DayEligibility, ...]
    status: str
    reason: str

    def __post_init__(self):
        object.__setattr__(self, "days", tuple(self.days))
        if (self.phase not in dict(PHASE_DAY_COUNTS)
                or self.scheduled_day_count != dict(PHASE_DAY_COUNTS)[self.phase]
                or self.minimum_day_count != dict(PHASE_MINIMUM_DAYS)[self.phase]
                or self.supplied_day_count != len(self.days)
                or self.supplied_day_count > self.scheduled_day_count
                or len({d.study_period_index for d in self.days}) != len(self.days)
                or len({d.utc_date for d in self.days}) != len(self.days)
                or self.eligible_day_count != sum(d.status == "ELIGIBLE" for d in self.days)
                or self.status != ("ADEQUATE" if self.eligible_day_count >= self.minimum_day_count
                                   else "COVERAGE_LIMITED")):
            raise ValueError("invalid phase coverage contract")


def phase_coverage(days, phase) -> PhaseCoverage:
    days = _cohort(days, phase)
    eligibility = tuple(day_eligibility(day) for day in days)
    eligible = sum(d.status == "ELIGIBLE" for d in eligibility)
    minimum = dict(PHASE_MINIMUM_DAYS)[phase]
    return PhaseCoverage(phase, dict(PHASE_DAY_COUNTS)[phase], len(days), eligible, minimum,
                         eligibility, "ADEQUATE" if eligible >= minimum else "COVERAGE_LIMITED",
                         "PHASE_DAY_GATE_PASSED" if eligible >= minimum else "INSUFFICIENT_ELIGIBLE_DAYS")


@dataclass(frozen=True)
class ConfigDevelopmentSample:
    identity: CandidateConfigIdentity
    days: tuple[AlignedStudyDay, ...]

    def __post_init__(self):
        object.__setattr__(self, "days", _cohort(self.days, "development", self.identity))


@dataclass(frozen=True)
class ExcludedDevelopmentDay:
    study_period_index: int
    reason: str


@dataclass(frozen=True)
class CommonDevelopmentSamples:
    samples: tuple[ConfigDevelopmentSample, ...]
    excluded_days: tuple[ExcludedDevelopmentDay, ...]
    version: str = "historical-market-state-common-development-samples-v1"
    samples_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "samples", tuple(self.samples))
        object.__setattr__(self, "excluded_days", tuple(self.excluded_days))
        _fixed_family_identities(s.identity for s in self.samples)
        if self.version != "historical-market-state-common-development-samples-v1":
            raise ValueError("invalid fixed common-sample family")
        seal_hash(self, "samples_sha256")


def common_development_samples(fixed_configs, days) -> CommonDevelopmentSamples:
    """Continuous configs share exact rows; event configs share eligible days."""
    identities = tuple(sorted(fixed_configs, key=lambda identity: identity.config_version))
    spec = _fixed_family_identities(identities)
    days = tuple(days)
    if any(_day_identity(day) not in identities or day.phase != "development" for day in days):
        raise ValueError("development common samples contain unregistered configs/other phases")
    if (any(len({d.utc_date for d in days if d.study_period_index == index}) != 1
            for index in {d.study_period_index for d in days})
            or any(len({d.study_period_index for d in days if d.utc_date == value}) != 1
                   for value in {d.utc_date for d in days})):
        raise ValueError("development configs disagree on study period/date identities")
    grouped = {identity: _cohort(tuple(d for d in days if _day_identity(d) == identity),
                                "development", identity) for identity in identities}
    indices = sorted({d.study_period_index for d in days})
    if len(indices) > dict(PHASE_DAY_COUNTS)["development"]:
        raise ValueError("too many distinct development study days")
    per_config = {identity: {d.study_period_index: d for d in grouped[identity]} for identity in identities}
    retained, excluded = {identity: [] for identity in identities}, []
    for index in indices:
        available = [per_config[identity].get(index) for identity in identities]
        if len({day.utc_date for day in available if day is not None}) > 1:
            raise ValueError("same study period has conflicting dates across configs")
        if any(day is None for day in available):
            excluded.append(ExcludedDevelopmentDay(index, "MISSING_CONFIG_DAY"))
            continue
        if spec.observation_mode == "CONTINUOUS":
            if len({d.scheduled_primary_count for d in available}) != 1:
                raise ValueError("continuous configs disagree on scheduled primary grid")
            lookups = [{row.observation_key: row for row in day.observations} for day in available]
            keys = sorted(set.intersection(*(set(lookup) for lookup in lookups)))
            for key in keys:
                rows = [lookup[key] for lookup in lookups]
                if len({(row.decision_time_ms, row.baseline_features, row.outcome) for row in rows}) != 1:
                    raise ValueError("common row key has different baseline/time/outcome across configs")
            available = [replace(day, observations=tuple(lookup[key] for key in keys))
                         for day, lookup in zip(available, lookups)]
        if any(day_eligibility(day).status != "ELIGIBLE" for day in available):
            excluded.append(ExcludedDevelopmentDay(index, "COMMON_DAY_COVERAGE_FAILED"))
            continue
        for identity, day in zip(identities, available):
            retained[identity].append(day)
    return CommonDevelopmentSamples(tuple(ConfigDevelopmentSample(i, tuple(retained[i]))
                                          for i in identities), tuple(excluded))


@dataclass(frozen=True)
class DayPredictiveResult:
    study_period_index: int
    utc_date: date
    observation_keys: tuple[str, ...]
    observation_count: int
    baseline_loss: float
    extended_loss: float
    delta: float

    def __post_init__(self):
        object.__setattr__(self, "observation_keys", tuple(self.observation_keys))
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date
                or not self.observation_keys or len(set(self.observation_keys)) != len(self.observation_keys)
                or self.observation_count != len(self.observation_keys)
                or finite_number(self.delta) != incremental_effect(self.baseline_loss, self.extended_loss)):
            raise ValueError("invalid paired day loss/effect")
        for key in self.observation_keys:
            require_name(key)


def _validate_day_results(coverage, results):
    expected = tuple((d.study_period_index, d.utc_date, d.usable_count)
                     for d in coverage.days if d.status == "ELIGIBLE")
    actual = tuple((d.study_period_index, d.utc_date, d.observation_count) for d in results)
    if actual != expected:
        raise ValueError("paired day results do not match eligible coverage days/row counts")


@dataclass(frozen=True)
class DevelopmentFold:
    held_out_period: int
    training_periods: tuple[int, ...]
    status: str
    reason: str
    day_result: DayPredictiveResult | None = None
    baseline_preprocessing: FrozenStandardizer | None = None
    candidate_preprocessing: FrozenStandardizer | None = None
    baseline_model_sha256: str | None = None
    extended_model_sha256: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "training_periods", tuple(self.training_periods))
        if (self.held_out_period in self.training_periods
                or len(set(self.training_periods)) != len(self.training_periods)
                or not self.training_periods or self.status not in ("EVALUABLE", "FIT_FAILED")):
            raise ValueError("invalid whole-day LODO fold")
        if self.status == "EVALUABLE":
            if (self.day_result is None or self.day_result.study_period_index != self.held_out_period
                    or self.baseline_preprocessing is None or self.candidate_preprocessing is None):
                raise ValueError("successful fold requires paired scores and preprocessing")
            require_sha256(self.baseline_model_sha256)
            require_sha256(self.extended_model_sha256)
        elif self.day_result is not None:
            raise ValueError("failed fit cannot manufacture a day effect")


@dataclass(frozen=True)
class DevelopmentConfigResult:
    identity: CandidateConfigIdentity
    coverage: PhaseCoverage
    folds: tuple[DevelopmentFold, ...]
    development_sample_sha256: str
    common_samples_sha256: str
    status: str
    reason: str
    plan_sha256: str = FROZEN_EVALUATION_PLAN.plan_sha256
    version: str = "historical-market-state-development-config-result-v1"
    median_delta: float | None = field(init=False)
    result_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "folds", tuple(self.folds))
        require_sha256(self.development_sample_sha256)
        require_sha256(self.common_samples_sha256)
        if (self.coverage.phase != "development" or self.plan_sha256 != FROZEN_EVALUATION_PLAN.plan_sha256
                or self.status not in ("EVALUABLE", "COVERAGE_LIMITED", "FIT_FAILED")
                or self.version != "historical-market-state-development-config-result-v1"):
            raise ValueError("invalid development evaluation identity/status")
        if self.status == "EVALUABLE":
            if (self.coverage.status != "ADEQUATE" or len(self.folds) != self.coverage.eligible_day_count
                    or any(f.status != "EVALUABLE" for f in self.folds)
                    or len({f.held_out_period for f in self.folds}) != len(self.folds)):
                raise ValueError("development score needs every eligible LODO fold")
            _validate_day_results(self.coverage, tuple(f.day_result for f in self.folds))
            indices = tuple(f.held_out_period for f in self.folds)
            if any(f.training_periods != tuple(i for i in indices if i != f.held_out_period) for f in self.folds):
                raise ValueError("LODO training masks differ from the other eligible development days")
            value = finite_number(median(f.day_result.delta for f in self.folds))
        else:
            if (self.status == "COVERAGE_LIMITED") != (self.coverage.status == "COVERAGE_LIMITED"):
                raise ValueError("development failure status conflicts with coverage")
            if self.status == "COVERAGE_LIMITED" and self.folds:
                raise ValueError("coverage-limited development must not fit LODO models")
            if self.status == "FIT_FAILED" and not any(f.status == "FIT_FAILED" for f in self.folds):
                raise ValueError("development fit failure requires explicit failed folds")
            value = None
        object.__setattr__(self, "median_delta", value)
        seal_hash(self, "result_sha256")


@dataclass(frozen=True)
class DevelopmentNomination:
    family_id: str
    development_result_refs: tuple[tuple[str, str], ...]
    common_samples_sha256: str
    status: str
    reason: str
    identity: CandidateConfigIdentity | None = None
    median_delta: float | None = None
    selected_result_sha256: str | None = None
    development_sample_sha256: str | None = None
    version: str = "historical-market-state-development-nomination-v1"
    nomination_sha256: str = field(init=False)

    def __post_init__(self):
        predictive_family(self.family_id)
        require_sha256(self.common_samples_sha256)
        refs = tuple(sorted(tuple(pair) for pair in self.development_result_refs))
        object.__setattr__(self, "development_result_refs", refs)
        if (not refs or len({c for c, _ in refs}) != len(refs)
                or self.status not in ("NOMINATED", "NOT_EVALUABLE")
                or self.version != "historical-market-state-development-nomination-v1"):
            raise ValueError("invalid nomination contract")
        for config, digest in refs:
            require_name(config)
            require_sha256(digest)
        if self.status == "NOMINATED":
            if (self.identity is None or self.identity.family_id != self.family_id
                    or (self.identity.config_version, self.selected_result_sha256) not in refs):
                raise ValueError("nomination must bind an existing evaluated configuration")
            finite_number(self.median_delta)
            require_sha256(self.development_sample_sha256)
        elif any(v is not None for v in (self.identity, self.median_delta,
                                         self.selected_result_sha256, self.development_sample_sha256)):
            raise ValueError("unavailable nomination cannot manufacture a selected config/effect")
        seal_hash(self, "nomination_sha256")


@dataclass(frozen=True)
class FrozenPredictivePair:
    identity: CandidateConfigIdentity
    horizon_minutes: int
    outcome_id: str
    outcome_kind: str
    training_periods: tuple[tuple[int, date], ...]
    baseline_preprocessing: FrozenStandardizer
    candidate_preprocessing: FrozenStandardizer
    baseline_model: FrozenLinearModel | FrozenLogisticModel
    extended_model: FrozenLinearModel | FrozenLogisticModel
    development_median: float
    nomination_sha256: str
    development_sample_sha256: str
    common_samples_sha256: str
    plan_sha256: str = FROZEN_EVALUATION_PLAN.plan_sha256
    version: str = "historical-market-state-frozen-predictive-pair-v1"
    pair_sha256: str = field(init=False)

    def __post_init__(self):
        spec = predictive_family(self.identity.family_id)
        object.__setattr__(self, "training_periods", tuple(tuple(p) for p in self.training_periods))
        if (self.baseline_preprocessing.features != BASELINE_FEATURES
                or self.candidate_preprocessing.features != spec.candidate_features
                or (self.horizon_minutes, self.outcome_id, self.outcome_kind)
                != (spec.horizon_minutes, spec.outcome_id, spec.outcome_kind)
                or self.plan_sha256 != FROZEN_EVALUATION_PLAN.plan_sha256
                or self.version != "historical-market-state-frozen-predictive-pair-v1"
                or not 8 <= len(self.training_periods) <= 10
                or any(type(i) is not int or i < 0 or type(d) is not date for i, d in self.training_periods)
                or tuple(sorted(self.training_periods)) != self.training_periods
                or len({p for p, _ in self.training_periods}) != len(self.training_periods)
                or len({d for _, d in self.training_periods}) != len(self.training_periods)):
            raise ValueError("invalid frozen development predictive identity/schema")
        baseline_names = self.baseline_preprocessing.active_feature_names
        candidate_names = self.candidate_preprocessing.active_feature_names
        model_type = FrozenLogisticModel if self.outcome_kind == "BINARY" else FrozenLinearModel
        if (not isinstance(self.baseline_model, model_type) or not isinstance(self.extended_model, model_type)
                or self.baseline_model.feature_names != ("intercept",) + baseline_names
                or self.extended_model.feature_names != ("intercept",) + baseline_names + candidate_names
                or any(item.numpy_version != FROZEN_EVALUATION_PLAN.numpy_version for item in (
                    self.baseline_preprocessing, self.candidate_preprocessing,
                    self.baseline_model, self.extended_model))):
            raise ValueError("frozen models differ from preprocessing/design/numerical environment")
        finite_number(self.development_median)
        require_sha256(self.nomination_sha256)
        require_sha256(self.development_sample_sha256)
        require_sha256(self.common_samples_sha256)
        seal_hash(self, "pair_sha256")


def _fit_models(identity, training_days):
    spec = predictive_family(identity.family_id)
    baseline_days = tuple(tuple(row.baseline_features for row in d.observations) for d in training_days)
    candidate_days = tuple(tuple(row.candidate_features for row in d.observations) for d in training_days)
    baseline = fit_standardizer(BASELINE_FEATURES, baseline_days)
    candidate = fit_standardizer(spec.candidate_features, candidate_days)
    rows = tuple(row for day in training_days for row in day.observations)
    baseline_x = tuple(baseline.active_row(row.baseline_features) for row in rows)
    extended_x = tuple(b + candidate.active_row(row.candidate_features) for b, row in zip(baseline_x, rows))
    targets = tuple(row.outcome for row in rows)
    weights = day_balanced_weights(tuple(len(day.observations) for day in training_days))
    fit = fit_weighted_logistic if spec.outcome_kind == "BINARY" else fit_weighted_ols
    baseline_model = fit(baseline.active_feature_names, baseline_x, targets, weights)
    extended_model = fit(baseline.active_feature_names + candidate.active_feature_names,
                         extended_x, targets, weights)
    return baseline, candidate, baseline_model, extended_model


def _score_day(day, baseline, candidate, baseline_model, extended_model):
    rows = tuple(sorted(day.observations, key=lambda row: row.observation_key))
    baseline_x = tuple(baseline.active_row(row.baseline_features) for row in rows)
    extended_x = tuple(b + candidate.active_row(row.candidate_features) for b, row in zip(baseline_x, rows))
    targets = tuple(row.outcome for row in rows)
    baseline_loss = day_loss(day.outcome_kind, targets, baseline_model.predict(baseline_x))
    extended_loss = day_loss(day.outcome_kind, targets, extended_model.predict(extended_x))
    return DayPredictiveResult(day.study_period_index, day.utc_date,
                               tuple(row.observation_key for row in rows), len(rows),
                               baseline_loss, extended_loss, incremental_effect(baseline_loss, extended_loss))


def evaluate_development_config(identity: CandidateConfigIdentity, days,
                                common_samples_sha256: str) -> DevelopmentConfigResult:
    require_sha256(common_samples_sha256)
    days = _cohort(days, "development", identity)
    coverage = phase_coverage(days, "development")
    eligible = tuple(d for d in days if day_eligibility(d).status == "ELIGIBLE")
    sample_sha = scientific_sha256(eligible)
    if coverage.status != "ADEQUATE":
        return DevelopmentConfigResult(identity, coverage, (), sample_sha, common_samples_sha256,
                                       "COVERAGE_LIMITED", coverage.reason)
    folds = []
    for held_out in eligible:
        training = tuple(day for day in eligible if day.study_period_index != held_out.study_period_index)
        training_indices = tuple(day.study_period_index for day in training)
        try:
            baseline, candidate, baseline_model, extended_model = _fit_models(identity, training)
            result = _score_day(held_out, baseline, candidate, baseline_model, extended_model)
            folds.append(DevelopmentFold(held_out.study_period_index, training_indices, "EVALUABLE",
                                          "LODO_SCORED", result, baseline, candidate,
                                          baseline_model.model_sha256, extended_model.model_sha256))
        except (StudyModelFitError, ValueError) as exc:
            folds.append(DevelopmentFold(held_out.study_period_index, training_indices, "FIT_FAILED", str(exc)))
    success = all(fold.status == "EVALUABLE" for fold in folds)
    return DevelopmentConfigResult(identity, coverage, tuple(folds), sample_sha, common_samples_sha256,
                                   "EVALUABLE" if success else "FIT_FAILED",
                                   "ALL_LODO_FOLDS_SCORED" if success else "LODO_MODEL_NOT_FEASIBLE")


def nominate_configuration(results) -> DevelopmentNomination:
    results = tuple(results)
    _fixed_family_identities(r.identity for r in results)
    if len({r.common_samples_sha256 for r in results}) != 1:
        raise ValueError("config results bind different family common-sample SHAs")
    family = results[0].identity.family_id
    common_sha = results[0].common_samples_sha256
    refs = tuple((r.identity.config_version, r.result_sha256) for r in results)
    eligible = tuple(r for r in results if r.status == "EVALUABLE")
    if not eligible:
        return DevelopmentNomination(family, refs, common_sha, "NOT_EVALUABLE", "NO_FINITE_COMPLETE_LODO_CONFIG")
    winner = min(eligible, key=lambda r: (-r.median_delta, r.identity.config_version))
    return DevelopmentNomination(family, refs, common_sha, "NOMINATED", "LARGEST_MEDIAN_RAW_DAY_DELTA",
                                 winner.identity, winner.median_delta, winner.result_sha256,
                                 winner.development_sample_sha256)


def evaluate_development_family(fixed_configs, days):
    samples = common_development_samples(fixed_configs, days)
    results = tuple(evaluate_development_config(sample.identity, sample.days, samples.samples_sha256)
                    for sample in samples.samples)
    return samples, results, nominate_configuration(results)


def fit_final_development_pair(nomination: DevelopmentNomination, days) -> FrozenPredictivePair:
    if nomination.status != "NOMINATED":
        raise ValueError("final fit requires a development nomination")
    days = _cohort(days, "development", nomination.identity)
    if phase_coverage(days, "development").status != "ADEQUATE":
        raise StudyModelFitError("final development fit lacks eligible days")
    eligible = tuple(d for d in days if day_eligibility(d).status == "ELIGIBLE")
    if scientific_sha256(eligible) != nomination.development_sample_sha256:
        raise ValueError("final fit rows do not match the nominated development sample")
    baseline, candidate, baseline_model, extended_model = _fit_models(nomination.identity, eligible)
    spec = predictive_family(nomination.family_id)
    return FrozenPredictivePair(nomination.identity, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind,
                                tuple((d.study_period_index, d.utc_date) for d in eligible),
                                baseline, candidate, baseline_model, extended_model,
                                nomination.median_delta, nomination.nomination_sha256,
                                nomination.development_sample_sha256, nomination.common_samples_sha256)


@dataclass(frozen=True)
class DevelopmentFreeze:
    study_manifest_sha256: str
    config_results: tuple[DevelopmentConfigResult, ...]
    nominations: tuple[DevelopmentNomination, ...]
    predictive_pairs: tuple[FrozenPredictivePair, ...]
    common_samples: tuple[CommonDevelopmentSamples, ...]
    plan: StudyEvaluationPlan = FROZEN_EVALUATION_PLAN
    layer_one_terciles: tuple[tuple[str, TercileSpec], ...] = ()
    hmm_cross_fit_sha256: str | None = None
    hmm_final_model_sha256: str | None = None
    version: str = "historical-market-state-development-freeze-v1"
    freeze_sha256: str = field(init=False)

    def __post_init__(self):
        require_sha256(self.study_manifest_sha256)
        for name, key in (("config_results", lambda r: (r.identity.family_id, r.identity.config_version)),
                          ("nominations", lambda n: n.family_id),
                          ("predictive_pairs", lambda p: p.identity.family_id),
                          ("common_samples", lambda s: s.samples[0].identity.family_id)):
            values = tuple(sorted(getattr(self, name), key=key))
            if len({key(value) for value in values}) != len(values):
                raise ValueError("duplicate development freeze member")
            object.__setattr__(self, name, values)
        terciles = tuple(sorted((tuple(p) for p in self.layer_one_terciles), key=lambda p: p[0]))
        object.__setattr__(self, "layer_one_terciles", terciles)
        if (self.plan != FROZEN_EVALUATION_PLAN or not self.config_results
                or len({name for name, _ in terciles}) != len(terciles)
                or self.version != "historical-market-state-development-freeze-v1"
                or any(r.plan_sha256 != self.plan.plan_sha256 for r in self.config_results)
                or {r.identity.family_id for r in self.config_results} != {n.family_id for n in self.nominations}
                or {r.identity.family_id for r in self.config_results}
                != {s.samples[0].identity.family_id for s in self.common_samples}):
            raise ValueError("invalid development freeze plan/family membership")
        if any(r.identity.family_id == "EXP-75-09" for r in self.config_results):
            require_sha256(self.hmm_cross_fit_sha256)
            require_sha256(self.hmm_final_model_sha256)
        for value in (self.hmm_cross_fit_sha256, self.hmm_final_model_sha256):
            if value is not None:
                require_sha256(value)
        for name, bins in terciles:
            require_name(name)
            if not isinstance(bins, TercileSpec):
                raise ValueError("development bins must be frozen tercile contracts")
        expected_pairs = set()
        for nomination in self.nominations:
            results = tuple(r for r in self.config_results if r.identity.family_id == nomination.family_id)
            if nomination != nominate_configuration(results):
                raise ValueError("freeze nomination differs from exact development nomination")
            common = next(s for s in self.common_samples if s.samples[0].identity.family_id == nomination.family_id)
            retained = common_development_samples(tuple(s.identity for s in common.samples),
                                                  tuple(day for s in common.samples for day in s.days))
            if retained.samples != common.samples:
                raise ValueError("freeze common samples violate the family paired-row/day selection rules")
            if nomination.common_samples_sha256 != common.samples_sha256:
                raise ValueError("freeze common-sample identity differs from the family selection object")
            if {r.identity for r in results} != {s.identity for s in common.samples}:
                raise ValueError("freeze config identities differ from the common-sample family")
            for result in results:
                sample = next(s for s in common.samples if s.identity == result.identity)
                if (result.common_samples_sha256 != common.samples_sha256
                        or result.development_sample_sha256 != scientific_sha256(sample.days)
                        or result.coverage != phase_coverage(sample.days, "development")):
                    raise ValueError("freeze config result does not bind its common-sample rows/coverage")
                if result.status == "EVALUABLE" and any(
                    fold.day_result.observation_keys != tuple(sorted(row.observation_key for row in day.observations))
                    for fold, day in zip(result.folds, sample.days)):
                    raise ValueError("freeze LODO masks differ from the common-sample observations")
            if nomination.status != "NOMINATED":
                continue
            expected_pairs.add(nomination.identity)
            pair = next((p for p in self.predictive_pairs if p.identity == nomination.identity), None)
            selected = next(r for r in results if r.result_sha256 == nomination.selected_result_sha256)
            if (pair is None or pair.nomination_sha256 != nomination.nomination_sha256
                    or pair.development_sample_sha256 != nomination.development_sample_sha256
                    or pair.common_samples_sha256 != common.samples_sha256
                    or pair.development_median != nomination.median_delta
                    or tuple(p for p, _ in pair.training_periods)
                    != tuple(f.held_out_period for f in selected.folds)):
                raise ValueError("final pair does not bind the selected development result/rows")
        if {p.identity for p in self.predictive_pairs} != expected_pairs:
            raise ValueError("development freeze contains missing or un-nominated final pairs")
        seal_hash(self, "freeze_sha256")


@dataclass(frozen=True)
class ValidationDecision:
    identity: CandidateConfigIdentity
    predictive_pair_sha256: str
    development_median: float
    coverage: PhaseCoverage
    day_results: tuple[DayPredictiveResult, ...]
    status: str
    reason: str
    version: str = "historical-market-state-validation-decision-v1"
    median_delta: float | None = field(init=False)
    decision_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "day_results", tuple(self.day_results))
        require_sha256(self.predictive_pair_sha256)
        finite_number(self.development_median)
        if (self.coverage.phase != "validation"
                or self.status not in ("CONFIRMED", "NOT_CONFIRMED", "COVERAGE_LIMITED", "FIT_FAILED")
                or self.version != "historical-market-state-validation-decision-v1"):
            raise ValueError("invalid validation phase/status")
        if self.status in ("CONFIRMED", "NOT_CONFIRMED"):
            if self.coverage.status != "ADEQUATE" or len(self.day_results) != self.coverage.eligible_day_count:
                raise ValueError("validation decision requires the covered paired days")
            _validate_day_results(self.coverage, self.day_results)
            value = finite_number(median(d.delta for d in self.day_results))
            confirmed = self.development_median > 0 and value > 0
            if (self.status == "CONFIRMED") != confirmed:
                raise ValueError("validation status conflicts with development/validation signs")
        else:
            if self.day_results:
                raise ValueError("failed validation cannot manufacture effects")
            if (self.status == "COVERAGE_LIMITED") != (self.coverage.status == "COVERAGE_LIMITED"):
                raise ValueError("validation failure status conflicts with coverage")
            value = None
        object.__setattr__(self, "median_delta", value)
        seal_hash(self, "decision_sha256")


def _evaluate_frozen_phase(pair, days, phase):
    days = _cohort(days, phase, pair.identity)
    training_indices = {index for index, _ in pair.training_periods}
    training_dates = {day_date for _, day_date in pair.training_periods}
    if any(day.study_period_index in training_indices or day.utc_date in training_dates for day in days):
        raise ValueError("evaluation days overlap the frozen development training cohort")
    coverage = phase_coverage(days, phase)
    if coverage.status != "ADEQUATE":
        return coverage, (), "COVERAGE_LIMITED", coverage.reason
    try:
        results = tuple(_score_day(day, pair.baseline_preprocessing, pair.candidate_preprocessing,
                                   pair.baseline_model, pair.extended_model)
                        for day in days if day_eligibility(day).status == "ELIGIBLE")
    except (StudyModelFitError, ValueError) as exc:
        return coverage, (), "FIT_FAILED", str(exc)
    return coverage, results, "EVALUABLE", "FROZEN_DEVELOPMENT_PAIR_SCORED"


def evaluate_validation(pair: FrozenPredictivePair, days) -> ValidationDecision:
    coverage, results, status, reason = _evaluate_frozen_phase(pair, days, "validation")
    if status == "EVALUABLE":
        confirmed = pair.development_median > 0 and median(d.delta for d in results) > 0
        status = "CONFIRMED" if confirmed else "NOT_CONFIRMED"
        reason = "POSITIVE_DEVELOPMENT_AND_VALIDATION" if confirmed else "NONPOSITIVE_OR_REVERSED_EFFECT"
    return ValidationDecision(pair.identity, pair.pair_sha256, pair.development_median,
                              coverage, results, status, reason)


@dataclass(frozen=True)
class ValidationFreeze:
    development_freeze_sha256: str
    decisions: tuple[ValidationDecision, ...]
    version: str = "historical-market-state-validation-freeze-v1"
    freeze_sha256: str = field(init=False)

    def __post_init__(self):
        require_sha256(self.development_freeze_sha256)
        object.__setattr__(self, "decisions", tuple(sorted(self.decisions, key=lambda d: d.identity.family_id)))
        if (len({d.identity.family_id for d in self.decisions}) != len(self.decisions)
                or self.version != "historical-market-state-validation-freeze-v1"):
            raise ValueError("duplicate or incompatible validation freeze member")
        seal_hash(self, "freeze_sha256")


def _validate_freeze_link(development: DevelopmentFreeze, validation: ValidationFreeze):
    if validation.development_freeze_sha256 != development.freeze_sha256:
        raise ValueError("validation scientific parent SHA mismatch")
    if ({(d.identity, d.predictive_pair_sha256, d.development_median) for d in validation.decisions}
            != {(p.identity, p.pair_sha256, p.development_median) for p in development.predictive_pairs}):
        raise ValueError("validation must bind exactly the nominated frozen predictive pairs")


def freeze_validation(development: DevelopmentFreeze, decisions) -> ValidationFreeze:
    result = ValidationFreeze(development.freeze_sha256, tuple(decisions))
    _validate_freeze_link(development, result)
    return result


@dataclass(frozen=True)
class TestAuthorization:
    development_freeze_sha256: str
    validation_freeze_sha256: str
    identity: CandidateConfigIdentity
    horizon_minutes: int
    outcome_id: str
    predictive_pair_sha256: str
    validation_decision_sha256: str
    authorized: bool = True
    confirmation_status: str = "CONFIRMED"
    version: str = "historical-market-state-test-authorization-v1"
    authorization_sha256: str = field(init=False)

    def __post_init__(self):
        for digest in (self.development_freeze_sha256, self.validation_freeze_sha256,
                       self.predictive_pair_sha256, self.validation_decision_sha256):
            require_sha256(digest)
        spec = predictive_family(self.identity.family_id)
        if (type(self.authorized) is not bool or not self.authorized
                or self.confirmation_status != "CONFIRMED"
                or type(self.horizon_minutes) is not int
                or (self.horizon_minutes, self.outcome_id) != (spec.horizon_minutes, spec.outcome_id)
                or self.version != "historical-market-state-test-authorization-v1"):
            raise ValueError("test authorization requires exact confirmed primary identity")
        seal_hash(self, "authorization_sha256")


def authorize_test(development: DevelopmentFreeze, validation: ValidationFreeze,
                   family_id: str) -> TestAuthorization:
    _validate_freeze_link(development, validation)
    predictive_family(family_id)
    decision = next((d for d in validation.decisions if d.identity.family_id == family_id), None)
    pair = next((p for p in development.predictive_pairs if p.identity.family_id == family_id), None)
    if decision is None or pair is None or decision.status != "CONFIRMED":
        raise ValueError("only development-nominated, validation-confirmed hypotheses unlock test")
    if (family_id == "EXP-75-09" and
            (development.hmm_cross_fit_sha256 is None or development.hmm_final_model_sha256 is None)):
        raise ValueError("HMM test authorization requires externally supplied cross-fit/final-model SHAs")
    return TestAuthorization(development.freeze_sha256, validation.freeze_sha256, pair.identity,
                             pair.horizon_minutes, pair.outcome_id, pair.pair_sha256,
                             decision.decision_sha256)


@dataclass(frozen=True)
class PrimaryTestResult:
    authorization: TestAuthorization
    coverage: PhaseCoverage
    day_results: tuple[DayPredictiveResult, ...]
    bootstrap: DayBootstrapResult | None
    sign_test: DaySignTestResult | None
    status: str
    reason: str
    classification: EvidenceClassification
    version: str = "historical-market-state-primary-test-result-v1"
    result_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "day_results", tuple(self.day_results))
        if (self.coverage.phase != "test" or self.status not in ("EVALUABLE", "COVERAGE_LIMITED", "FIT_FAILED")
                or self.version != "historical-market-state-primary-test-result-v1"):
            raise ValueError("invalid authorized primary test status")
        if self.status == "EVALUABLE":
            if (self.coverage.status != "ADEQUATE" or len(self.day_results) != self.coverage.eligible_day_count
                    or self.bootstrap is None or self.sign_test is None
                    or self.bootstrap.day_effects != tuple(d.delta for d in self.day_results)
                    or self.sign_test != exact_day_sign_test(self.bootstrap.day_effects)
                    or (self.bootstrap.namespace.phase, self.bootstrap.namespace.family_id,
                        self.bootstrap.namespace.config_version, self.bootstrap.namespace.horizon_minutes,
                        self.bootstrap.namespace.outcome_id)
                    != ("test", self.authorization.identity.family_id, self.authorization.identity.config_version,
                        self.authorization.horizon_minutes, self.authorization.outcome_id)):
                raise ValueError("test statistics do not bind the authorized paired day effects")
            _validate_day_results(self.coverage, self.day_results)
        else:
            if self.day_results or self.bootstrap is not None or self.sign_test is not None:
                raise ValueError("failed test evaluation cannot manufacture effects/statistics")
            if (self.status == "COVERAGE_LIMITED") != (self.coverage.status == "COVERAGE_LIMITED"):
                raise ValueError("test failure status conflicts with coverage")
            if self.classification != EvidenceClassification.COVERAGE_LIMITED:
                raise ValueError("failed test evaluation must retain feasibility classification")
        seal_hash(self, "result_sha256")


def evaluate_test(development: DevelopmentFreeze, validation: ValidationFreeze,
                  authorization: TestAuthorization, days) -> PrimaryTestResult:
    if not isinstance(authorization, TestAuthorization):
        raise ValueError("test requires an immutable authorization contract")
    expected = authorize_test(development, validation, authorization.identity.family_id)
    if authorization != expected:
        raise ValueError("test authorization identity/scientific parent mismatch")
    pair = next(p for p in development.predictive_pairs if p.identity == authorization.identity)
    decision = next(d for d in validation.decisions if d.identity == authorization.identity)
    coverage, results, status, reason = _evaluate_frozen_phase(pair, days, "test")
    bootstrap = sign_test = None
    if status == "EVALUABLE":
        effects = tuple(d.delta for d in results)
        namespace = BootstrapNamespace(development.study_manifest_sha256, "test", pair.identity.family_id,
                                       pair.identity.config_version, pair.horizon_minutes, pair.outcome_id)
        bootstrap = whole_day_bootstrap(effects, namespace)
        sign_test = exact_day_sign_test(effects)
    classification = classify_evidence(EvidenceComponents(
        coverage.status, "FIT_FAILED" if status == "FIT_FAILED" else "FEASIBLE",
        pair.development_median, decision.median_delta,
        bootstrap.median if bootstrap is not None else None,
        bootstrap.median_ci95 if bootstrap is not None else None))
    return PrimaryTestResult(authorization, coverage, results, bootstrap, sign_test, status, reason, classification)
