"""Part-C day losses, whole-day uncertainty and conservative classifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import math
from statistics import fmean
import numpy as np

from .historical_market_state_study_features import (
    FROZEN_EVALUATION_PLAN, PHASES, finite_number, finite_vector, predictive_family,
    require_name, require_sha256, seal_hash,
)


BOOTSTRAP_DRAWS = 10_000
BOOTSTRAP_GENERATOR = "numpy.random.PCG64"
QUANTILE_METHOD = "linear"


def squared_error(actual, prediction) -> float:
    difference = finite_number(actual) - finite_number(prediction)
    return finite_number(difference * difference)


def brier_loss(actual, probability) -> float:
    actual, probability = finite_number(actual), finite_number(probability)
    if actual not in (0.0, 1.0) or not 0 <= probability <= 1:
        raise ValueError("Brier loss requires binary outcomes and probabilities")
    return squared_error(actual, probability)


def day_loss(outcome_kind, actual, predictions) -> float:
    actual, predictions = finite_vector(actual), finite_vector(predictions)
    if not actual or len(actual) != len(predictions):
        raise ValueError("day loss requires nonempty paired rows")
    if outcome_kind not in ("CONTINUOUS", "BINARY"):
        raise ValueError("unknown primary outcome kind")
    loss = squared_error if outcome_kind == "CONTINUOUS" else brier_loss
    return finite_number(fmean(loss(a, p) for a, p in zip(actual, predictions)))


def incremental_effect(baseline_loss, extended_loss) -> float:
    baseline_loss, extended_loss = finite_number(baseline_loss), finite_number(extended_loss)
    if min(baseline_loss, extended_loss) < 0:
        raise ValueError("losses cannot be negative")
    return finite_number(baseline_loss - extended_loss)


def relative_loss_improvement(baseline_loss, extended_loss) -> float | None:
    delta = incremental_effect(baseline_loss, extended_loss)
    return delta / baseline_loss if baseline_loss > 0 else None


@dataclass(frozen=True)
class BootstrapNamespace:
    study_manifest_sha256: str
    phase: str
    family_id: str
    config_version: str
    horizon_minutes: int
    outcome_id: str
    statistic: str = "median_delta"

    def __post_init__(self):
        require_sha256(self.study_manifest_sha256)
        require_name(self.config_version)
        spec = predictive_family(self.family_id)
        if (self.phase not in PHASES or type(self.horizon_minutes) is not int
                or (self.horizon_minutes, self.outcome_id) != (spec.horizon_minutes, spec.outcome_id)
                or self.statistic not in ("median_delta", "mean_delta")):
            raise ValueError("invalid bootstrap scientific namespace")

    @property
    def seed(self) -> int:
        namespace = ("crypto-watcher:historical-market-state-study-v1:part-c-v1:"
                     + self.study_manifest_sha256 + ":" + self.phase + ":" + self.family_id
                     + ":" + self.config_version + ":" + str(self.horizon_minutes)
                     + ":" + self.outcome_id + ":" + self.statistic)
        return int.from_bytes(hashlib.sha256(namespace.encode("utf-8")).digest()[:16], "big")


@dataclass(frozen=True)
class DayBootstrapResult:
    namespace: BootstrapNamespace
    day_effects: tuple[float, ...]
    median: float
    mean: float
    median_ci95: tuple[float, float]
    mean_ci95: tuple[float, float]
    positive_day_count: int
    positive_day_fraction: float
    day_count: int
    seed: int
    draws: int = BOOTSTRAP_DRAWS
    generator: str = BOOTSTRAP_GENERATOR
    quantile_method: str = QUANTILE_METHOD
    numpy_version: str = np.__version__
    version: str = "historical-market-state-whole-day-bootstrap-v1"
    result_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "day_effects", finite_vector(self.day_effects))
        for name in ("median_ci95", "mean_ci95"):
            values = finite_vector(getattr(self, name))
            if len(values) != 2 or values[0] > values[1]:
                raise ValueError("invalid bootstrap confidence interval")
            object.__setattr__(self, name, values)
        for value in (self.median, self.mean, self.positive_day_fraction):
            finite_number(value)
        if (not self.day_effects or self.day_count != len(self.day_effects)
                or self.median != float(np.median(self.day_effects))
                or self.mean != float(np.mean(self.day_effects))
                or self.positive_day_count != sum(v > 0 for v in self.day_effects)
                or self.positive_day_fraction != self.positive_day_count / self.day_count
                or self.seed != self.namespace.seed or self.draws != BOOTSTRAP_DRAWS
                or self.generator != BOOTSTRAP_GENERATOR or self.quantile_method != "linear"
                or self.numpy_version != FROZEN_EVALUATION_PLAN.numpy_version
                or self.version != "historical-market-state-whole-day-bootstrap-v1"):
            raise ValueError("incompatible bootstrap identity/counts")
        seal_hash(self, "result_sha256")


def whole_day_bootstrap(day_effects, namespace: BootstrapNamespace) -> DayBootstrapResult:
    if np.__version__ != FROZEN_EVALUATION_PLAN.numpy_version:
        raise ValueError("bootstrap NumPy version differs from the frozen evaluation plan")
    effects = finite_vector(day_effects)
    if not effects:
        raise ValueError("whole-day bootstrap needs day effects")
    values = np.asarray(effects)
    generator = np.random.Generator(np.random.PCG64(namespace.seed))
    indices = generator.integers(0, len(effects), size=(BOOTSTRAP_DRAWS, len(effects)))
    draws = values[indices]
    medians, means = np.median(draws, axis=1), np.mean(draws, axis=1)
    median_ci = tuple(float(v) for v in np.quantile(medians, (0.025, 0.975), method="linear"))
    mean_ci = tuple(float(v) for v in np.quantile(means, (0.025, 0.975), method="linear"))
    positive = sum(v > 0 for v in effects)
    return DayBootstrapResult(namespace, effects, float(np.median(values)), float(np.mean(values)),
                              median_ci, mean_ci, positive, positive / len(effects),
                              len(effects), namespace.seed, numpy_version=np.__version__)


@dataclass(frozen=True)
class DaySignTestResult:
    nonzero_day_count: int
    positive_day_count: int
    raw_p: float
    status: str
    version: str = "historical-market-state-exact-one-sided-day-sign-v1"

    def __post_init__(self):
        n, k = self.nonzero_day_count, self.positive_day_count
        if type(n) is not int or type(k) is not int or not 0 <= k <= n:
            raise ValueError("invalid sign-test day counts")
        expected = sum(math.comb(n, j) for j in range(k, n + 1)) / (2 ** n) if n else 1.0
        if (finite_number(self.raw_p) != expected
                or self.status != ("TESTABLE" if n else "NO_NONZERO_DAYS")
                or self.version != "historical-market-state-exact-one-sided-day-sign-v1"):
            raise ValueError("incompatible exact sign-test result")


def exact_day_sign_test(day_effects) -> DaySignTestResult:
    effects = finite_vector(day_effects)
    n, k = sum(v != 0 for v in effects), sum(v > 0 for v in effects)
    if n == 0:
        return DaySignTestResult(0, 0, 1.0, "NO_NONZERO_DAYS")
    probability = sum(math.comb(n, j) for j in range(k, n + 1)) / (2 ** n)
    return DaySignTestResult(n, k, probability, "TESTABLE")


@dataclass(frozen=True)
class HypothesisPValue:
    hypothesis_id: str
    raw_p: float | None
    status: str = "TESTABLE"

    def __post_init__(self):
        require_name(self.hypothesis_id)
        if self.raw_p is not None and not 0 <= finite_number(self.raw_p) <= 1:
            raise ValueError("p-value outside [0,1]")
        if self.status not in ("TESTABLE", "NOT_TESTABLE", "VETOED", "MISSING"):
            raise ValueError("unknown primary test status")
        if self.status == "TESTABLE" and self.raw_p is None:
            raise ValueError("testable hypothesis requires a p-value")


@dataclass(frozen=True)
class HolmMember:
    hypothesis_id: str
    raw_p: float
    adjusted_p: float
    status: str


def holm_adjust(fixed_hypothesis_ids, supplied: tuple[HypothesisPValue, ...]) -> tuple[HolmMember, ...]:
    family = tuple(fixed_hypothesis_ids)
    if not family or len(set(family)) != len(family):
        raise ValueError("Holm requires a fixed complete unique family")
    for identity in family:
        require_name(identity)
    supplied = tuple(supplied)
    if (len({p.hypothesis_id for p in supplied}) != len(supplied)
            or any(p.hypothesis_id not in family for p in supplied)):
        raise ValueError("duplicate or nonfamily p-value")
    lookup = {p.hypothesis_id: p for p in supplied}
    members = tuple(lookup.get(identity, HypothesisPValue(identity, None, "MISSING"))
                    for identity in family)
    ordered = sorted(members, key=lambda p: (p.raw_p if p.status == "TESTABLE" else 1.0,
                                            p.hypothesis_id))
    adjusted, previous = {}, 0.0
    for index, item in enumerate(ordered):
        raw = item.raw_p if item.status == "TESTABLE" else 1.0
        previous = min(1.0, max(previous, (len(family) - index) * raw))
        adjusted[item.hypothesis_id] = HolmMember(item.hypothesis_id, raw, previous, item.status)
    return tuple(adjusted[identity] for identity in sorted(family))


class EvidenceClassification(str, Enum):
    ROBUST_INCREMENTAL_EVIDENCE = "ROBUST_INCREMENTAL_EVIDENCE"
    STATE_QUALITY_ONLY = "STATE_QUALITY_ONLY"
    REDUNDANT_WITH_V1 = "REDUNDANT_WITH_V1"
    UNSTABLE_ACROSS_PERIODS = "UNSTABLE_ACROSS_PERIODS"
    COVERAGE_LIMITED = "COVERAGE_LIMITED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class EvidenceComponents:
    coverage_status: str
    model_status: str
    development_median: float | None = None
    validation_median: float | None = None
    test_median: float | None = None
    test_median_ci95: tuple[float, float] | None = None
    phase_sign_reversal: bool = False
    track_a_supported: bool = False
    exact_information_redundancy: bool = False

    def __post_init__(self):
        if (self.coverage_status not in ("ADEQUATE", "COVERAGE_LIMITED")
                or self.model_status not in ("FEASIBLE", "FIT_FAILED")):
            raise ValueError("component feasibility status must be explicit")
        for name in ("development_median", "validation_median", "test_median"):
            if getattr(self, name) is not None:
                finite_number(getattr(self, name))
        if self.test_median_ci95 is not None:
            interval = finite_vector(self.test_median_ci95)
            if len(interval) != 2 or interval[0] > interval[1]:
                raise ValueError("invalid median confidence interval")
            object.__setattr__(self, "test_median_ci95", interval)
        if any(type(v) is not bool for v in (self.phase_sign_reversal,
                                            self.track_a_supported, self.exact_information_redundancy)):
            raise ValueError("classification flags must be explicit booleans")


def classify_evidence(components: EvidenceComponents) -> EvidenceClassification:
    c = components
    if c.coverage_status != "ADEQUATE" or c.model_status != "FEASIBLE":
        return EvidenceClassification.COVERAGE_LIMITED
    positive = all(v is not None and v > 0 for v in (
        c.development_median, c.validation_median, c.test_median))
    if (positive and c.test_median_ci95 is not None and c.test_median_ci95[0] > 0
            and not c.phase_sign_reversal):
        return EvidenceClassification.ROBUST_INCREMENTAL_EVIDENCE
    if (c.development_median is not None and c.development_median > 0
            and c.validation_median is not None
            and (c.validation_median <= 0 or
                 (c.validation_median > 0 and c.test_median is not None and c.test_median <= 0))):
        return EvidenceClassification.UNSTABLE_ACROSS_PERIODS
    if c.track_a_supported:
        return EvidenceClassification.STATE_QUALITY_ONLY
    if c.exact_information_redundancy:
        return EvidenceClassification.REDUNDANT_WITH_V1
    return EvidenceClassification.INCONCLUSIVE
