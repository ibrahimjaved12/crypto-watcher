"""Explicit day-balanced preprocessing, weighted OLS and unpenalized logit.

NumPy arrays exist only inside calculations; fitted scientific contracts use
finite scalars/tuples. No outcome-driven support selection or estimator fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np

from .historical_market_state_study_features import (
    FeatureSpec, finite_vector, require_name, seal_hash,
    validate_feature_vector,
)


OLS_RCOND = 1e-12
LOGISTIC_MAX_ITERATIONS = 100
LOGISTIC_GRADIENT_TOLERANCE = 1e-8
LOGISTIC_MAX_BACKTRACKING_REDUCTIONS = 30
LOGISTIC_SOLVER_VERSION = "historical-market-state-weighted-logit-newton-v1"


class StudyModelFitError(ValueError):
    """Explicit unsupported rank, separation, convergence or finite arithmetic."""


def _finite(array, context):
    if not np.all(np.isfinite(array)):
        raise StudyModelFitError(f"non-finite {context}")
    return array


def day_balanced_weights(day_counts) -> tuple[float, ...]:
    counts = tuple(day_counts)
    if not counts or any(type(n) is not int or n <= 0 for n in counts):
        raise StudyModelFitError("training requires nonempty days")
    return tuple(1 / (len(counts) * n) for n in counts for _ in range(n))


@dataclass(frozen=True)
class FrozenStandardizer:
    features: tuple[FeatureSpec, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    active_mask: tuple[bool, ...]
    support_reasons: tuple[str, ...]
    numpy_version: str = np.__version__
    version: str = "historical-market-state-day-balanced-standardizer-v1"
    standardizer_sha256: str = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "features", tuple(self.features))
        object.__setattr__(self, "means", finite_vector(self.means))
        object.__setattr__(self, "scales", finite_vector(self.scales))
        object.__setattr__(self, "active_mask", tuple(self.active_mask))
        object.__setattr__(self, "support_reasons", tuple(self.support_reasons))
        n = len(self.features)
        if (any(not isinstance(f, FeatureSpec) for f in self.features)
                or len({f.name for f in self.features}) != n
                or any(len(v) != n for v in (self.means, self.scales, self.active_mask,
                                             self.support_reasons))
                or any(type(a) is not bool for a in self.active_mask)
                or any(s <= 0 for s in self.scales)
                or self.version != "historical-market-state-day-balanced-standardizer-v1"):
            raise ValueError("invalid frozen standardizer")
        for feature, mean, scale, active, reason in zip(
                self.features, self.means, self.scales, self.active_mask, self.support_reasons):
            if (reason != ("ACTIVE" if active else "ZERO_VARIANCE")
                    or (not active and (feature.processing != "STANDARDIZE" or scale != 1.0))
                    or (feature.processing == "BINARY" and (mean, scale, active) != (0.0, 1.0, True))):
                raise ValueError("invalid structural support/unchanged binary column")
        require_name(self.numpy_version)
        seal_hash(self, "standardizer_sha256")

    @property
    def active_feature_names(self):
        return tuple(f.name for f, active in zip(self.features, self.active_mask) if active)

    def transform(self, row) -> tuple[float, ...]:
        row = validate_feature_vector(row, self.features)
        result = tuple(0.0 if not active else value if f.processing == "BINARY"
                       else (value - mean) / scale
                       for value, f, mean, scale, active in zip(
                           row, self.features, self.means, self.scales, self.active_mask))
        return finite_vector(result)

    def active_row(self, row) -> tuple[float, ...]:
        return tuple(v for v, a in zip(self.transform(row), self.active_mask) if a)


def fit_standardizer(features: tuple[FeatureSpec, ...], day_rows) -> FrozenStandardizer:
    features = tuple(features)
    days = tuple(tuple(validate_feature_vector(row, features) for row in day) for day in day_rows)
    weights = np.asarray(day_balanced_weights(tuple(len(day) for day in days)))
    matrix = np.asarray(tuple(row for day in days for row in day), dtype=float)
    means, scales, active, reasons = [], [], [], []
    for index, feature in enumerate(features):
        if feature.processing == "BINARY":
            mean, scale, supported = 0.0, 1.0, True
        else:
            values = matrix[:, index]
            if np.all(values == values[0]):
                mean, scale, supported = float(values[0]), 1.0, False
            else:
                mean = float(np.average(values, weights=weights))
                with np.errstate(over="ignore", invalid="ignore"):
                    variance = float(np.average((values - mean) ** 2, weights=weights))
                _finite((mean, variance), "standardizer moments")
                if variance <= 0:
                    raise StudyModelFitError("nonconstant numeric variance lacks floating-point support")
                scale, supported = float(np.sqrt(variance)), True
        means.append(mean)
        scales.append(scale)
        active.append(supported)
        reasons.append("ACTIVE" if supported else "ZERO_VARIANCE")
    return FrozenStandardizer(features, tuple(means), tuple(scales), tuple(active), tuple(reasons))


def _design(feature_names, rows, targets, weights):
    names = tuple(feature_names)
    for name in names:
        require_name(name)
    if len(set(names)) != len(names) or "intercept" in names:
        raise StudyModelFitError("feature names must be unique and exclude intercept")
    vectors = tuple(finite_vector(row) for row in rows)
    y = np.asarray(finite_vector(targets), dtype=float)
    w = np.asarray(finite_vector(weights), dtype=float)
    if (not vectors or any(len(row) != len(names) for row in vectors)
            or len(y) != len(vectors) or len(w) != len(vectors) or np.any(w <= 0)):
        raise StudyModelFitError("invalid finite weighted design dimensions")
    total = float(np.sum(w))
    _finite(total, "weight sum")
    w = w / total
    x = np.column_stack((np.ones(len(vectors)), np.asarray(vectors, dtype=float)))
    return ("intercept",) + names, x, y, w


def _lstsq(x, y):
    try:
        solution, _, rank, singular = np.linalg.lstsq(x, y, rcond=OLS_RCOND)
    except np.linalg.LinAlgError as exc:
        raise StudyModelFitError("SVD did not converge") from exc
    _finite(solution, "least-squares solution")
    _finite(singular, "singular values")
    if rank != x.shape[1]:
        raise StudyModelFitError("rank-deficient active design/Hessian")
    return solution, int(rank), tuple(float(v) for v in singular)


def _validate_model(model):
    object.__setattr__(model, "feature_names", tuple(model.feature_names))
    object.__setattr__(model, "coefficients", finite_vector(model.coefficients))
    if (not model.feature_names or model.feature_names[0] != "intercept"
            or len(model.feature_names) != len(model.coefficients)
            or len(set(model.feature_names)) != len(model.feature_names)):
        raise ValueError("invalid frozen coefficients/design")
    for name in model.feature_names:
        require_name(name)
    require_name(model.numpy_version)


def _predict_linear(model, rows):
    vectors = tuple(finite_vector(row) for row in rows)
    if any(len(row) != len(model.feature_names) - 1 for row in vectors):
        raise ValueError("prediction dimensions differ from fitted active columns")
    if not vectors:
        return np.empty(0)
    x = np.column_stack((np.ones(len(vectors)), np.asarray(vectors, dtype=float)))
    with np.errstate(over="ignore", invalid="ignore"):
        return _finite(x @ np.asarray(model.coefficients), "prediction")


@dataclass(frozen=True)
class FrozenLinearModel:
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    rank: int
    singular_values: tuple[float, ...]
    rcond: float = OLS_RCOND
    numpy_version: str = np.__version__
    version: str = "historical-market-state-weighted-ols-v1"
    model_sha256: str = field(init=False)

    def __post_init__(self):
        _validate_model(self)
        object.__setattr__(self, "singular_values", finite_vector(self.singular_values))
        if (type(self.rank) is not int or self.rank != len(self.feature_names)
                or len(self.singular_values) != self.rank
                or any(s <= 0 for s in self.singular_values) or self.rcond != OLS_RCOND
                or self.version != "historical-market-state-weighted-ols-v1"):
            raise ValueError("invalid frozen OLS rank/solver diagnostics")
        seal_hash(self, "model_sha256")

    def predict(self, rows) -> tuple[float, ...]:
        return tuple(float(v) for v in _predict_linear(self, rows))


def fit_weighted_ols(feature_names, rows, targets, weights) -> FrozenLinearModel:
    names, x, y, w = _design(feature_names, rows, targets, weights)
    root = np.sqrt(w)
    coefficients, rank, singular = _lstsq(root[:, None] * x, root * y)
    return FrozenLinearModel(names, tuple(float(v) for v in coefficients), rank, singular)


def _sigmoid(eta):
    result = np.empty_like(eta)
    positive = eta >= 0
    result[positive] = 1 / (1 + np.exp(-eta[positive]))
    exp_eta = np.exp(eta[~positive])
    result[~positive] = exp_eta / (1 + exp_eta)
    return _finite(result, "sigmoid")


def _negative_log_likelihood(x, y, w, beta):
    with np.errstate(over="ignore", invalid="ignore"):
        eta = _finite(x @ beta, "logistic linear predictor")
        return float(_finite(np.sum(w * (np.logaddexp(0, eta) - y * eta)), "logistic objective"))


@dataclass(frozen=True)
class FrozenLogisticModel:
    feature_names: tuple[str, ...]
    coefficients: tuple[float, ...]
    iterations: int
    convergence_tolerance: float = LOGISTIC_GRADIENT_TOLERANCE
    solver_version: str = LOGISTIC_SOLVER_VERSION
    max_iterations: int = LOGISTIC_MAX_ITERATIONS
    max_backtracking_reductions: int = LOGISTIC_MAX_BACKTRACKING_REDUCTIONS
    rcond: float = OLS_RCOND
    numpy_version: str = np.__version__
    version: str = "historical-market-state-unpenalized-logistic-v1"
    model_sha256: str = field(init=False)

    def __post_init__(self):
        _validate_model(self)
        if (type(self.iterations) is not int or not 0 <= self.iterations <= 100
                or (self.convergence_tolerance, self.solver_version, self.max_iterations,
                    self.max_backtracking_reductions, self.rcond, self.version)
                != (1e-8, LOGISTIC_SOLVER_VERSION, 100, 30, 1e-12,
                    "historical-market-state-unpenalized-logistic-v1")):
            raise ValueError("invalid frozen logistic solver diagnostics")
        seal_hash(self, "model_sha256")

    def predict(self, rows) -> tuple[float, ...]:
        return tuple(float(v) for v in _sigmoid(_predict_linear(self, rows)))


def fit_weighted_logistic(feature_names, rows, targets, weights) -> FrozenLogisticModel:
    names, x, y, w = _design(feature_names, rows, targets, weights)
    if not np.all((y == 0) | (y == 1)) or len(np.unique(y)) != 2:
        raise StudyModelFitError("logistic training requires both exact binary classes")
    _lstsq(np.sqrt(w)[:, None] * x, np.zeros(len(y)))
    beta = np.zeros(x.shape[1])
    for iteration in range(LOGISTIC_MAX_ITERATIONS + 1):
        eta = _finite(x @ beta, "logistic predictor")
        # This coefficient vector is itself a complete/quasi-separation
        # certificate. A small gradient alone cannot establish a finite MLE.
        margins = (2 * y - 1) * eta
        if np.all(margins >= 0) and np.any(margins > 0):
            raise StudyModelFitError("complete or quasi separation; no finite unpenalized fit")
        probability = _sigmoid(eta)
        gradient = _finite(x.T @ (w * (probability - y)), "logistic gradient")
        hessian = _finite(x.T @ ((w * probability * (1 - probability))[:, None] * x),
                          "weighted Hessian")
        step, _, _ = _lstsq(hessian, gradient)
        if float(np.max(np.abs(gradient))) <= LOGISTIC_GRADIENT_TOLERANCE:
            return FrozenLogisticModel(names, tuple(float(v) for v in beta), iteration)
        if iteration == LOGISTIC_MAX_ITERATIONS:
            break
        objective = _negative_log_likelihood(x, y, w, beta)
        for reduction in range(LOGISTIC_MAX_BACKTRACKING_REDUCTIONS + 1):
            candidate = _finite(beta - (0.5 ** reduction) * step, "logistic Newton step")
            if _negative_log_likelihood(x, y, w, candidate) < objective:
                beta = candidate
                break
        else:
            raise StudyModelFitError("logistic backtracking failed to improve objective")
    raise StudyModelFitError("unpenalized logistic regression did not converge in 100 iterations")
