"""Frozen Part-C schemas and serialization-independent aligned observations.

This boundary accepts finite, already-extracted primary evidence. It performs
no artifact parsing, market calculations, event joining or HMM inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import date
from fractions import Fraction
import hashlib
import json
import math

from .historical_experiment_batch import report_json_safe
from .historical_market_state_study import (
    PRIMARY_HYPOTHESES, STUDY_VERSION, DEVELOPMENT_PERIOD_COUNT,
    VALIDATION_PERIOD_COUNT, TEST_PERIOD_COUNT,
)


PART_C_PLAN_VERSION = "historical-market-state-study-part-c-plan-v1"
ALIGNED_OBSERVATION_VERSION = "historical-market-state-aligned-observation-v1"
PHASES = ("development", "validation", "test")
PHASE_DAY_COUNTS = (("development", DEVELOPMENT_PERIOD_COUNT),
                    ("validation", VALIDATION_PERIOD_COUNT),
                    ("test", TEST_PERIOD_COUNT))
PHASE_MINIMUM_DAYS = (("development", 8), ("validation", 6), ("test", 9))


def canonical_json(value) -> str:
    return json.dumps(report_json_safe(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def scientific_sha256(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def seal_hash(value, attribute: str) -> None:
    content = {item.name: getattr(value, item.name)
               for item in fields(value) if item.name != attribute}
    object.__setattr__(value, attribute, scientific_sha256(content))


def require_sha256(value: str) -> None:
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("scientific identity must be a lowercase SHA-256")


def finite_number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a finite numeric scalar")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("numeric scalar exceeds finite floating-point support") from exc
    if not math.isfinite(number):
        raise ValueError("expected a finite numeric scalar")
    return number


def finite_vector(values) -> tuple[float, ...]:
    return tuple(finite_number(value) for value in values)


def require_name(value: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError("expected a nonempty stable identity")


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    processing: str

    def __post_init__(self):
        require_name(self.name)
        if self.processing not in ("STANDARDIZE", "BINARY"):
            raise ValueError("unknown frozen feature processing")


_BASELINE_NAMES = (
    "v1_broad_rise", "v1_broad_drop", "v1_median_normalized_movement",
    "v1_breadth_rising", "v1_breadth_falling", "v1_material_breadth_rising",
    "v1_material_breadth_falling", "v1_dispersion_mad_normalized_movement",
    "v1_median_rvol", "v1_median_acceleration", "v1_pace_accelerating",
    "v1_pace_decelerating", "utc_06_12", "utc_12_18", "utc_18_24",
)
_BINARY_BASELINE = frozenset((
    "v1_broad_rise", "v1_broad_drop", "v1_pace_accelerating",
    "v1_pace_decelerating", "utc_06_12", "utc_12_18", "utc_18_24",
))
BASELINE_FEATURES = tuple(FeatureSpec(
    name, "BINARY" if name in _BINARY_BASELINE else "STANDARDIZE")
    for name in _BASELINE_NAMES)


@dataclass(frozen=True)
class FamilyEvaluationSpec:
    family_id: str
    causal_forward_test: bool
    observation_mode: str
    horizon_minutes: int | None
    outcome_id: str
    outcome_kind: str | None
    candidate_features: tuple[FeatureSpec, ...]
    expected_config_count: int

    def __post_init__(self):
        object.__setattr__(self, "candidate_features", tuple(self.candidate_features))
        if any(not isinstance(f, FeatureSpec) for f in self.candidate_features):
            raise ValueError("candidate schema requires FeatureSpec values")
        if len({f.name for f in self.candidate_features}) != len(self.candidate_features):
            raise ValueError("duplicate candidate feature")
        expected_count = (0 if self.family_id == "EXP-75-04A" else
                          1 if self.family_id in (
                              "EXP-75-09", "EXP-75-10", "EXP-75-11-OI", "EXP-75-11-FUNDING",
                              "EXP-75-11-LIQUIDATION", "EXP-75-12") else 3)
        if type(self.expected_config_count) is not int or self.expected_config_count != expected_count:
            raise ValueError("configuration count differs from the frozen family cardinality")
        if self.causal_forward_test:
            if (self.observation_mode not in ("CONTINUOUS", "EVENT")
                    or type(self.horizon_minutes) is not int or self.horizon_minutes <= 0
                    or self.outcome_kind not in ("CONTINUOUS", "BINARY")
                    or not self.candidate_features):
                raise ValueError("invalid causal family schema")
        elif (self.family_id != "EXP-75-04A" or self.observation_mode != "RETROSPECTIVE"
              or self.horizon_minutes is not None or self.outcome_kind is not None
              or self.candidate_features):
            raise ValueError("PELT must remain retrospective only")


def _schema(*entries):
    return tuple(FeatureSpec(name, processing) for name, processing in entries)


_CANDIDATE_SCHEMAS = (
    _schema(("ewma_minus_raw_normalized_movement", "STANDARDIZE"),
            ("direction_disagreement", "BINARY")),
    _schema(("positive_accumulator", "STANDARDIZE"),
            ("negative_accumulator", "STANDARDIZE"), ("detector_down_shift", "BINARY")),
    _schema(("filtered_minus_raw_normalized_movement", "STANDARDIZE"),
            ("kalman_trend", "STANDARDIZE")),
    (),
    _schema(("recent_change_probability", "STANDARDIZE")),
    _schema(("candidate_minus_v1_median_acceleration", "STANDARDIZE")),
    _schema(("candidate_minus_v1_normalized_movement", "STANDARDIZE"),
            ("direction_disagreement", "BINARY")),
    _schema(("candidate_minus_v1_normalized_movement", "STANDARDIZE"),
            ("direction_disagreement", "BINARY")),
    _schema(("explained_variance_ratio", "STANDARDIZE"),
            ("current_pc1_energy_fraction", "STANDARDIZE")),
    _schema(("median_pairwise_correlation", "STANDARDIZE"),
            ("network_edge_density", "STANDARDIZE")),
    _schema(("hmm_low_movement", "BINARY"), ("hmm_high_movement", "BINARY"),
            ("posterior_entropy", "STANDARDIZE")),
    _schema(("median_signed_mark_trade_divergence_5m", "STANDARDIZE")),
    _schema(("median_log_oi_change_5m", "STANDARDIZE"),
            ("median_log_oi_change_15m", "STANDARDIZE")),
    _schema(("median_funding_per_hour", "STANDARDIZE")),
    _schema(("log1p_observed_total_notional_15m", "STANDARDIZE"),
            ("liquidation_imbalance_15m", "STANDARDIZE"),
            ("liquidation_breadth_15m", "STANDARDIZE")),
    _schema(("pooled_notional_imbalance_5m", "STANDARDIZE"),
            ("sign_breadth_5m", "STANDARDIZE")),
)
# Bind schemas to the frozen order rather than silently reassigning features if
# the independent primary registry is ever reordered or extended.
_FAMILY_ORDER = ("EXP-75-01", "EXP-75-02", "EXP-75-03", "EXP-75-04A", "EXP-75-04B",
                 "EXP-75-05", "EXP-75-06A", "EXP-75-06B", "EXP-75-07", "EXP-75-08",
                 "EXP-75-09", "EXP-75-10", "EXP-75-11-OI", "EXP-75-11-FUNDING",
                 "EXP-75-11-LIQUIDATION", "EXP-75-12")
if tuple(h.family_id for h in PRIMARY_HYPOTHESES) != _FAMILY_ORDER:
    raise ValueError("primary registry order differs from the frozen feature registry")
_BINARY_OUTCOMES = ("state_persistence", "v1_direction_persistence",
                    "persistence_weakening")
FAMILY_EVALUATION_SPECS = tuple(FamilyEvaluationSpec(
    hypothesis.family_id, hypothesis.causal_forward_test,
    ("RETROSPECTIVE" if not hypothesis.causal_forward_test else
     "EVENT" if hypothesis.family_id in ("EXP-75-02", "EXP-75-04B") else "CONTINUOUS"),
    hypothesis.primary_horizon_minutes, hypothesis.primary_outcome_id,
    (None if not hypothesis.causal_forward_test else
     "BINARY" if hypothesis.primary_outcome_id in _BINARY_OUTCOMES else "CONTINUOUS"),
    schema, 0 if not hypothesis.causal_forward_test else
    1 if hypothesis.family_id in ("EXP-75-09", "EXP-75-10", "EXP-75-11-OI",
                                  "EXP-75-11-FUNDING", "EXP-75-11-LIQUIDATION", "EXP-75-12") else 3)
    for hypothesis, schema in zip(PRIMARY_HYPOTHESES, _CANDIDATE_SCHEMAS))
if (len(FAMILY_EVALUATION_SPECS) != 16
        or tuple((s.family_id, s.causal_forward_test, s.horizon_minutes, s.outcome_id)
                 for s in FAMILY_EVALUATION_SPECS)
        != tuple((h.family_id, h.causal_forward_test, h.primary_horizon_minutes,
                  h.primary_outcome_id) for h in PRIMARY_HYPOTHESES)):
    raise ValueError("Part-C family registry conflicts with PRIMARY_HYPOTHESES")


@dataclass(frozen=True)
class StudyEvaluationPlan:
    baseline_features: tuple[FeatureSpec, ...] = BASELINE_FEATURES
    families: tuple[FamilyEvaluationSpec, ...] = FAMILY_EVALUATION_SPECS
    reference_states: tuple[str, ...] = ("NEUTRAL", "MIXED", "UTC_00_06", "MID_MOVEMENT")
    phase_day_counts: tuple[tuple[str, int], ...] = PHASE_DAY_COUNTS
    phase_minimum_days: tuple[tuple[str, int], ...] = PHASE_MINIMUM_DAYS
    scientific_policies: tuple[tuple[str, str], ...] = (
        ("baseline", "5m-V1;intercept-added-by-model"),
        ("preprocessing", "training-only;day-balanced-population-z-score;binary-unchanged"),
        ("support", "exact-zero-variance-numeric-inactive;rank-failure-explicit"),
        ("continuous_model_loss", "unpenalized-weighted-OLS;squared-error"),
        ("binary_model_loss", "unpenalized-weighted-logit;Brier"),
        ("outcomes", "unscaled;absolute-return;endpoint-persistence-binary;zero-neutral-breadth-extremity"),
        ("weakening", "PERSISTED=1;WEAKENED=0;REVERSED=0"),
        ("continuous_configs", "exact-common-observation-keys-before-eligibility"),
        ("event_configs", "common-eligible-days;native-independent-event-rows"),
        ("nomination", "median-raw-baseline-minus-extended-day-loss;exact-tie-lexicographic-config"),
        ("validation", "frozen-development-model;positive-development-and-validation;no-reselection"),
        ("terciles", "development-day-balanced-ECDF;1/3,2/3;smallest-value-reaching-mass;ties-lower"),
        ("uncertainty", "whole-day-replacement;median-and-mean-percentile-95CI"),
        ("sign_test", "exact-one-sided-positive-day;exclude-exact-zero;secondary-only"),
        ("multiplicity", "Holm-fixed-15-non-PELT;missing-vetoed-not-testable-p=1;secondary-only"),
        ("classification", "coverage;robust-positive-three-phases-and-test-median-CI;unstable;track-A;exact-redundancy;inconclusive"),
    )
    continuous_coverage_fraction: float = 0.5
    ols_rcond: float = 1e-12
    logistic_max_iterations: int = 100
    logistic_gradient_tolerance: float = 1e-8
    logistic_max_backtracking_reductions: int = 30
    bootstrap_draws: int = 10_000
    bootstrap_generator: str = "numpy.random.PCG64"
    bootstrap_seed_version: str = "crypto-watcher:historical-market-state-study-v1:part-c-v1"
    quantile_method: str = "linear"
    numpy_version: str = "2.2.6"
    study_version: str = STUDY_VERSION
    version: str = PART_C_PLAN_VERSION
    plan_sha256: str = field(init=False)

    def __post_init__(self):
        for name in ("baseline_features", "families", "reference_states",
                     "phase_day_counts", "phase_minimum_days", "scientific_policies"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if (self.baseline_features != BASELINE_FEATURES or self.families != FAMILY_EVALUATION_SPECS
                or self.reference_states != ("NEUTRAL", "MIXED", "UTC_00_06", "MID_MOVEMENT")
                or self.phase_day_counts != PHASE_DAY_COUNTS
                or self.phase_minimum_days != PHASE_MINIMUM_DAYS
                or self.scientific_policies != type(self).__dataclass_fields__["scientific_policies"].default
                or (self.continuous_coverage_fraction, self.ols_rcond,
                    self.logistic_max_iterations, self.logistic_gradient_tolerance,
                    self.logistic_max_backtracking_reductions, self.bootstrap_draws,
                    self.bootstrap_generator, self.bootstrap_seed_version,
                    self.quantile_method, self.numpy_version, self.study_version, self.version)
                != (0.5, 1e-12, 100, 1e-8, 30, 10_000, "numpy.random.PCG64",
                    "crypto-watcher:historical-market-state-study-v1:part-c-v1",
                    "linear", "2.2.6", STUDY_VERSION, PART_C_PLAN_VERSION)):
            raise ValueError("evaluation plan differs from the frozen v1 design")
        seal_hash(self, "plan_sha256")


FROZEN_EVALUATION_PLAN = StudyEvaluationPlan()


def family_spec(family_id: str) -> FamilyEvaluationSpec:
    for spec in FAMILY_EVALUATION_SPECS:
        if spec.family_id == family_id:
            return spec
    raise ValueError("unknown frozen family")


def predictive_family(family_id: str) -> FamilyEvaluationSpec:
    spec = family_spec(family_id)
    if not spec.causal_forward_test:
        raise ValueError("PELT is retrospective only and cannot enter predictive evaluation")
    return spec


def validate_feature_vector(values, schema) -> tuple[float, ...]:
    vector = finite_vector(values)
    if len(vector) != len(schema):
        raise ValueError("feature dimensions differ from the frozen schema")
    if any(f.processing == "BINARY" and value not in (0.0, 1.0)
           for value, f in zip(vector, schema)):
        raise ValueError("binary features must equal 0 or 1")
    return vector


@dataclass(frozen=True)
class AlignedStudyObservation:
    study_period_index: int
    utc_date: date
    phase: str
    family_id: str
    algorithm_version: str
    config_version: str
    observation_mode: str
    decision_time_ms: int
    horizon_minutes: int
    outcome_id: str
    outcome_kind: str
    observation_key: str
    baseline_features: tuple[float, ...]
    candidate_features: tuple[float, ...]
    outcome: float
    version: str = ALIGNED_OBSERVATION_VERSION

    def __post_init__(self):
        spec = predictive_family(self.family_id)
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date or self.phase not in PHASES
                or type(self.decision_time_ms) is not int or self.decision_time_ms < 0
                or type(self.horizon_minutes) is not int
                or (self.observation_mode, self.horizon_minutes, self.outcome_id, self.outcome_kind)
                != (spec.observation_mode, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind)
                or self.version != ALIGNED_OBSERVATION_VERSION):
            raise ValueError("invalid aligned primary observation identity")
        for name in (self.algorithm_version, self.config_version, self.observation_key):
            require_name(name)
        object.__setattr__(self, "baseline_features",
                           validate_feature_vector(self.baseline_features, BASELINE_FEATURES))
        object.__setattr__(self, "candidate_features",
                           validate_feature_vector(self.candidate_features, spec.candidate_features))
        object.__setattr__(self, "outcome", finite_number(self.outcome))
        if self.outcome_kind == "BINARY" and self.outcome not in (0.0, 1.0):
            raise ValueError("binary outcome must equal 0 or 1")


@dataclass(frozen=True)
class AlignedStudyDay:
    study_period_index: int
    utc_date: date
    phase: str
    family_id: str
    algorithm_version: str
    config_version: str
    observation_mode: str
    horizon_minutes: int
    outcome_id: str
    outcome_kind: str
    scheduled_primary_count: int | None
    observations: tuple[AlignedStudyObservation, ...]

    def __post_init__(self):
        spec = predictive_family(self.family_id)
        object.__setattr__(self, "observations", tuple(self.observations))
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date or self.phase not in PHASES
                or type(self.horizon_minutes) is not int
                or (self.observation_mode, self.horizon_minutes, self.outcome_id, self.outcome_kind)
                != (spec.observation_mode, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind)):
            raise ValueError("invalid aligned day identity")
        require_name(self.algorithm_version)
        require_name(self.config_version)
        if self.observation_mode == "CONTINUOUS":
            if (type(self.scheduled_primary_count) is not int
                    or self.scheduled_primary_count <= 0
                    or len(self.observations) > self.scheduled_primary_count):
                raise ValueError("continuous day requires a positive scheduled row count")
        elif self.scheduled_primary_count is not None:
            raise ValueError("event days do not have a scheduled grid count")
        identity_names = ("study_period_index", "utc_date", "phase", "family_id",
                          "algorithm_version", "config_version", "observation_mode",
                          "horizon_minutes", "outcome_id", "outcome_kind")
        if any(not isinstance(row, AlignedStudyObservation)
               or any(getattr(row, name) != getattr(self, name) for name in identity_names)
               for row in self.observations):
            raise ValueError("day observations mix scientific identities")
        if len({row.observation_key for row in self.observations}) != len(self.observations):
            raise ValueError("duplicate observation key inside one day")


def absolute_market_return(value) -> float:
    return abs(finite_number(value))


def future_breadth_extremity(per_symbol_returns) -> float:
    values = finite_vector(per_symbol_returns)
    if not values:
        raise ValueError("breadth requires at least one eligible finite return")
    return abs(sum(v > 0 for v in values) - sum(v < 0 for v in values)) / len(values)


def persistence_weakening_binary(value: str) -> float:
    if value == "PERSISTED":
        return 1.0
    if value in ("WEAKENED", "REVERSED"):
        return 0.0
    raise ValueError("unavailable/unknown persistence outcome")


@dataclass(frozen=True)
class EvidenceValueDay:
    """Generic extracted Layer-1 scalar evidence, with phase and day identity."""

    study_period_index: int
    utc_date: date
    phase: str
    values: tuple[float, ...]

    def __post_init__(self):
        object.__setattr__(self, "values", finite_vector(self.values))
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date or self.phase not in PHASES
                or not self.values):
            raise ValueError("invalid nonempty evidence day")


@dataclass(frozen=True)
class TercileSpec:
    q1: float
    q2: float
    observed_bins: tuple[str, ...]
    training_periods: tuple[int, ...]
    version: str = "historical-market-state-day-balanced-terciles-v1"
    tercile_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "q1", finite_number(self.q1))
        object.__setattr__(self, "q2", finite_number(self.q2))
        object.__setattr__(self, "observed_bins", tuple(self.observed_bins))
        object.__setattr__(self, "training_periods", tuple(self.training_periods))
        if (self.q1 > self.q2 or not self.training_periods
                or any(type(p) is not int or p < 0 for p in self.training_periods)
                or tuple(sorted(self.training_periods)) != self.training_periods
                or len(set(self.training_periods)) != len(self.training_periods)
                or not self.observed_bins or self.observed_bins[0] != "LOW"
                or self.observed_bins != tuple(b for b in ("LOW", "MID", "HIGH")
                                               if b in self.observed_bins)
                or (self.q1 == self.q2 and "MID" in self.observed_bins)
                or self.version != "historical-market-state-day-balanced-terciles-v1"):
            raise ValueError("invalid frozen tercile support")
        seal_hash(self, "tercile_sha256")

    def assign(self, value) -> str:
        value = finite_number(value)
        return "LOW" if value <= self.q1 else "MID" if value <= self.q2 else "HIGH"


def fit_day_balanced_terciles(days: tuple[EvidenceValueDay, ...]) -> TercileSpec:
    days = tuple(sorted(days, key=lambda day: day.study_period_index))
    if (not days or any(day.phase != "development" for day in days)
            or len({d.study_period_index for d in days}) != len(days)
            or len({d.utc_date for d in days}) != len(days)):
        raise ValueError("terciles fit unique development days only")
    # Rational masses keep exact 1/3 boundaries independent of roundoff/order.
    masses = {}
    for day in days:
        for value in day.values:
            masses[value] = masses.get(value, Fraction(0)) + Fraction(1, len(days) * len(day.values))
    cumulative = Fraction(0)
    cuts = []
    for value, mass in sorted(masses.items()):
        cumulative += mass
        while len(cuts) < 2 and cumulative >= Fraction(len(cuts) + 1, 3):
            cuts.append(value)
    q1, q2 = cuts
    observed = tuple(b for b in ("LOW", "MID", "HIGH") if any(
        ("LOW" if v <= q1 else "MID" if v <= q2 else "HIGH") == b for v in masses))
    return TercileSpec(q1, q2, observed, tuple(d.study_period_index for d in days))
