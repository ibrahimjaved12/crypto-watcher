"""Frozen Part-C schemas and serialization-independent aligned observations.

This boundary accepts finite, already-extracted primary evidence. It performs
no artifact parsing, market calculations, event joining or HMM inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timezone
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


# Compact identity copied from the finalized selection manifest; no artifact I/O.
FROZEN_STUDY_MANIFEST_SHA256 = '7f863cb6e6d3a39c46f109ba65c1617eb0aef894bb7dff74a6b393b24dc714f9'
_FROZEN_PERIODS = (
    (0, "2024-01-01", "development"),
    (1, "2024-02-06", "development"),
    (2, "2024-04-03", "development"),
    (3, "2024-05-02", "development"),
    (4, "2024-05-17", "development"),
    (5, "2024-06-29", "development"),
    (6, "2024-07-28", "development"),
    (7, "2024-09-09", "development"),
    (8, "2024-09-24", "development"),
    (9, "2024-11-13", "development"),
    (10, "2024-11-21", "validation"),
    (11, "2024-12-27", "validation"),
    (12, "2025-02-01", "validation"),
    (13, "2025-03-02", "validation"),
    (14, "2025-04-21", "validation"),
    (15, "2025-05-06", "validation"),
    (16, "2025-06-04", "validation"),
    (17, "2025-07-24", "validation"),
    (18, "2025-08-15", "test"),
    (19, "2025-09-13", "test"),
    (20, "2025-11-02", "test"),
    (21, "2025-12-08", "test"),
    (22, "2025-12-30", "test"),
    (23, "2026-01-28", "test"),
    (24, "2026-02-19", "test"),
    (25, "2026-04-03", "test"),
    (26, "2026-05-02", "test"),
    (27, "2026-06-21", "test"),
    (28, "2026-07-06", "test"),
    (29, "2026-08-25", "test"),
)


@dataclass(frozen=True)
class FrozenPeriodRoster:
    study_manifest_sha256: str = FROZEN_STUDY_MANIFEST_SHA256
    periods: tuple[tuple[int, date, str], ...] = tuple(
        (index, date.fromisoformat(day), phase) for index, day, phase in _FROZEN_PERIODS)
    roster_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "periods", tuple(tuple(p) for p in self.periods))
        if (self.study_manifest_sha256 != FROZEN_STUDY_MANIFEST_SHA256
                or self.periods != type(self).__dataclass_fields__["periods"].default):
            raise ValueError("period roster differs from the frozen study manifest")
        seal_hash(self, "roster_sha256")

    def require_period(self, index, utc_date, phase):
        if type(index) is not int or (index, utc_date, phase) not in self.periods:
            raise ValueError("period/date/phase differs from the frozen study roster")


FROZEN_PERIOD_ROSTER = FrozenPeriodRoster()


def validate_observation_timing(utc_date, decision_time_ms, mode, horizon_minutes):
    start = int(datetime.combine(utc_date, datetime.min.time(), timezone.utc).timestamp()) * 1000
    if not start <= decision_time_ms < start + 86_400_000:
        raise ValueError("decision timestamp lies outside the declared UTC study date")
    interval = horizon_minutes * 60_000 if mode == "CONTINUOUS" else 5_000
    if (decision_time_ms - start) % interval:
        raise ValueError("decision timestamp is off the exact primary/replay boundary grid")


@dataclass(frozen=True)
class FinalizedPeriodProvenance:
    study_period_index: int
    utc_date: date
    phase: str
    study_manifest_sha256: str
    extension_coverage_manifest_sha256: str
    part_b_report_schema_version: str
    part_b_code_revision: str
    period_report_sha256: str
    event_time_v1_context_version: str | None = None
    event_time_v1_context_sha256: str | None = None
    bocpd_onset_evidence_version: str | None = None
    bocpd_onset_evidence_sha256: str | None = None
    hmm_cross_fit_sha256: str | None = None
    hmm_final_model_sha256: str | None = None
    study_version: str = STUDY_VERSION
    provenance_sha256: str = field(init=False)

    def __post_init__(self):
        FROZEN_PERIOD_ROSTER.require_period(self.study_period_index, self.utc_date, self.phase)
        if (self.study_version != STUDY_VERSION
                or self.study_manifest_sha256 != FROZEN_STUDY_MANIFEST_SHA256
                or self.part_b_report_schema_version != "historical-market-state-study-period-report-v2"):
            raise ValueError("incompatible finalized Part-B study/report identity")
        for value in (self.study_manifest_sha256, self.extension_coverage_manifest_sha256,
                      self.period_report_sha256):
            require_sha256(value)
        require_name(self.part_b_code_revision)
        for version, digest, expected in (
            (self.event_time_v1_context_version, self.event_time_v1_context_sha256,
             "historical-market-state-event-time-v1-context-v1"),
            (self.bocpd_onset_evidence_version, self.bocpd_onset_evidence_sha256,
             "historical-market-state-bocpd-onset-evidence-v1")):
            if version is None and digest is None:
                continue
            if version != expected:
                raise ValueError("incompatible event-time V1 / BOCPD evidence version")
            require_sha256(digest)
        for digest in (self.hmm_cross_fit_sha256, self.hmm_final_model_sha256):
            if digest is not None:
                require_sha256(digest)
        seal_hash(self, "provenance_sha256")


@dataclass(frozen=True)
class PartBScientificContract:
    """Prerequisites known before test opening; contains no period-report hashes."""

    study_version: str
    study_manifest_sha256: str
    extension_coverage_manifest_sha256: str
    part_b_report_schema_version: str
    part_b_code_revision: str
    hmm_cross_fit_sha256: str | None = None
    hmm_final_model_sha256: str | None = None
    contract_sha256: str = field(init=False)

    def __post_init__(self):
        if (self.study_version != STUDY_VERSION
                or self.study_manifest_sha256 != FROZEN_STUDY_MANIFEST_SHA256
                or self.part_b_report_schema_version != "historical-market-state-study-period-report-v2"):
            raise ValueError("incompatible shared Part-B scientific contract")
        require_sha256(self.study_manifest_sha256)
        require_sha256(self.extension_coverage_manifest_sha256)
        require_name(self.part_b_code_revision)
        for digest in (self.hmm_cross_fit_sha256, self.hmm_final_model_sha256):
            if digest is not None:
                require_sha256(digest)
        seal_hash(self, "contract_sha256")


def shared_part_b_contract(periods, hmm_cross_fit_sha256=None, hmm_final_model_sha256=None):
    """Validate shared metadata, also usable for a supplied (incomplete) aligned cohort."""
    periods = tuple(periods)
    if not periods or any(not isinstance(p, FinalizedPeriodProvenance) or replace(p) != p for p in periods):
        raise ValueError("shared contract requires finalized period identities")
    names = ("study_version", "study_manifest_sha256", "extension_coverage_manifest_sha256",
             "part_b_report_schema_version", "part_b_code_revision")
    for name in names:
        if len({getattr(p, name) for p in periods}) != 1:
            raise ValueError("mixed Part-B scientific provenance: " + name)
    for name, expected in (("hmm_cross_fit_sha256", hmm_cross_fit_sha256),
                           ("hmm_final_model_sha256", hmm_final_model_sha256)):
        if any(getattr(p, name) is not None and getattr(p, name) != expected for p in periods):
            raise ValueError("mixed HMM scientific provenance: " + name)
    return PartBScientificContract(*(getattr(periods[0], name) for name in names),
                                   hmm_cross_fit_sha256, hmm_final_model_sha256)


@dataclass(frozen=True)
class UpstreamInputProvenance:
    phase: str
    periods: tuple[FinalizedPeriodProvenance, ...]
    hmm_cross_fit_sha256: str | None = None
    hmm_final_model_sha256: str | None = None
    roster: FrozenPeriodRoster = FROZEN_PERIOD_ROSTER
    version: str = "historical-market-state-part-c-upstream-inputs-v2"
    shared_contract: PartBScientificContract = field(init=False)
    provenance_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "periods", tuple(self.periods))
        if (self.roster != FROZEN_PERIOD_ROSTER or self.phase not in PHASES
                or self.version != "historical-market-state-part-c-upstream-inputs-v2"
                or any(not isinstance(p, FinalizedPeriodProvenance) or replace(p) != p for p in self.periods)
                or tuple((p.study_period_index, p.utc_date, p.phase) for p in self.periods)
                != tuple(p for p in self.roster.periods if p[2] == self.phase)):
            raise ValueError("upstream provenance requires the exact ordered frozen phase roster")
        object.__setattr__(self, "shared_contract", shared_part_b_contract(
            self.periods, self.hmm_cross_fit_sha256, self.hmm_final_model_sha256))
        seal_hash(self, "provenance_sha256")

    def require_source(self, index, digest):
        source = next((p for p in self.periods if p.study_period_index == index), None)
        if source is None or digest != source.provenance_sha256:
            raise ValueError("aligned input differs from finalized upstream period-report/evidence identity")


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
class LayerOneStratifierSpec:
    family_id: str
    mode: str
    source_identity: str
    candidate_feature_name: str | None = None
    allowed_categories: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "allowed_categories", tuple(self.allowed_categories))
        require_name(self.family_id)
        require_name(self.source_identity)
        if self.mode == "CONTINUOUS_TERCILE":
            if self.candidate_feature_name != self.source_identity or self.allowed_categories:
                raise ValueError("continuous Layer-1 source must name its exact candidate feature")
        elif self.mode == "NATIVE_CATEGORY":
            if self.candidate_feature_name is not None or not self.allowed_categories:
                raise ValueError("native Layer-1 source requires frozen categories")
            for category in self.allowed_categories:
                require_name(category)
            if len(set(self.allowed_categories)) != len(self.allowed_categories):
                raise ValueError("duplicate native Layer-1 category")
        else:
            raise ValueError("unknown Layer-1 mode")


# One declared Layer-1 source per predictive primary family. These refer to
# existing extracted features/native states, with no new candidate calculation.
_LAYER_ONE_CONTINUOUS_SOURCES = (
    ("EXP-75-01", "ewma_minus_raw_normalized_movement"),
    ("EXP-75-03", "kalman_trend"),
    ("EXP-75-05", "candidate_minus_v1_median_acceleration"),
    ("EXP-75-06A", "candidate_minus_v1_normalized_movement"),
    ("EXP-75-06B", "candidate_minus_v1_normalized_movement"),
    ("EXP-75-07", "explained_variance_ratio"),
    ("EXP-75-08", "median_pairwise_correlation"),
    ("EXP-75-10", "median_signed_mark_trade_divergence_5m"),
    ("EXP-75-11-OI", "median_log_oi_change_5m"),
    ("EXP-75-11-FUNDING", "median_funding_per_hour"),
    ("EXP-75-11-LIQUIDATION", "log1p_observed_total_notional_15m"),
    ("EXP-75-12", "pooled_notional_imbalance_5m"),
)
_LAYER_ONE_NATIVE_SOURCES = (
    LayerOneStratifierSpec("EXP-75-02", "NATIVE_CATEGORY", "cusum_direction_state", None,
                          ("NONE", "UP_SHIFT", "DOWN_SHIFT", "AMBIGUOUS", "UNAVAILABLE")),
    LayerOneStratifierSpec("EXP-75-04B", "NATIVE_CATEGORY", "bocpd_observation.detector_state", None,
                          ("WARMING", "NONE", "CHANGE", "UNAVAILABLE")),
    LayerOneStratifierSpec("EXP-75-09", "NATIVE_CATEGORY", "hard_state", None,
                          ("LOW_MOVEMENT", "MID_MOVEMENT", "HIGH_MOVEMENT")),
)
_layer_one_sources = {
    family: LayerOneStratifierSpec(family, "CONTINUOUS_TERCILE", feature, feature)
    for family, feature in _LAYER_ONE_CONTINUOUS_SOURCES}
_layer_one_sources.update({spec.family_id: spec for spec in _LAYER_ONE_NATIVE_SOURCES})
LAYER_ONE_STRATIFIERS = tuple(_layer_one_sources[spec.family_id] for spec in FAMILY_EVALUATION_SPECS
                             if spec.causal_forward_test)
for spec in LAYER_ONE_STRATIFIERS:
    family = next(f for f in FAMILY_EVALUATION_SPECS if f.family_id == spec.family_id)
    if spec.mode == "CONTINUOUS_TERCILE" and (
            family.observation_mode != "CONTINUOUS"
            or spec.candidate_feature_name not in tuple(f.name for f in family.candidate_features)):
        raise ValueError("Layer-1 registry conflicts with frozen candidate semantics")


def layer_one_stratifier(family_id):
    spec = next((s for s in LAYER_ONE_STRATIFIERS if s.family_id == family_id), None)
    if spec is None:
        raise ValueError("no predictive Layer-1 stratifier for this family")
    return spec


def require_layer_one_spec(identity, spec):
    if not isinstance(spec, LayerOneStratifierSpec) or spec != layer_one_stratifier(identity.family_id):
        raise ValueError("Layer-1 family/mode/source differs from the frozen registry")
    if spec.mode != "CONTINUOUS_TERCILE":
        raise ValueError("native-category Layer-1 entries cannot fit continuous terciles")


@dataclass(frozen=True)
class StudyEvaluationPlan:
    baseline_features: tuple[FeatureSpec, ...] = BASELINE_FEATURES
    families: tuple[FamilyEvaluationSpec, ...] = FAMILY_EVALUATION_SPECS
    layer_one_stratifiers: tuple[LayerOneStratifierSpec, ...] = LAYER_ONE_STRATIFIERS
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
        for name in ("baseline_features", "families", "layer_one_stratifiers", "reference_states",
                     "phase_day_counts", "phase_minimum_days", "scientific_policies"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if (self.baseline_features != BASELINE_FEATURES or self.families != FAMILY_EVALUATION_SPECS
                or self.layer_one_stratifiers != LAYER_ONE_STRATIFIERS
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
PRIMARY_CONFIRMATORY_FAMILY = tuple(s.family_id for s in FAMILY_EVALUATION_SPECS
                                   if s.causal_forward_test)


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
        validate_observation_timing(self.utc_date, self.decision_time_ms,
                                    self.observation_mode, self.horizon_minutes)
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
    source_provenance: FinalizedPeriodProvenance | None = None

    def __post_init__(self):
        spec = predictive_family(self.family_id)
        object.__setattr__(self, "observations", tuple(self.observations))
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or type(self.utc_date) is not date or self.phase not in PHASES
                or type(self.horizon_minutes) is not int
                or (self.observation_mode, self.horizon_minutes, self.outcome_id, self.outcome_kind)
                != (spec.observation_mode, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind)):
            raise ValueError("invalid aligned day identity")
        FROZEN_PERIOD_ROSTER.require_period(self.study_period_index, self.utc_date, self.phase)
        if self.source_provenance is not None:
            source = self.source_provenance
            if (not isinstance(source, FinalizedPeriodProvenance)
                    or replace(source) != source
                    or (source.study_period_index, source.utc_date, source.phase)
                    != (self.study_period_index, self.utc_date, self.phase)):
                raise ValueError("aligned day has mismatched source period provenance")
            if self.observation_mode == "EVENT" and source.event_time_v1_context_sha256 is None:
                raise ValueError("event aligned input requires exact event-time V1 provenance")
            if self.family_id == "EXP-75-04B" and source.bocpd_onset_evidence_sha256 is None:
                raise ValueError("BOCPD aligned input requires causal onset-evidence provenance")
            if self.family_id == "EXP-75-09":
                digest = (source.hmm_cross_fit_sha256 if self.phase == "development" else
                          source.hmm_final_model_sha256)
                if digest is None:
                    raise ValueError("HMM input requires phase-specific cross-fit/final-model provenance")
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
