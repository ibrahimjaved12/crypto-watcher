"""Pure, versioned market movement calculations over canonical #70 readiness.

The pure historical builder prepares returns and comparable notionals.
No clock, history acquisition, or provider access is performed here.
"""

from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
import math
from types import MappingProxyType
from typing import Generic, Mapping, TypeVar

from .movement import BUCKET_INTERVAL_MS, MovementReadiness


ALGORITHM_VERSION = "market-movement-v1"
DEFAULT_CONFIG_VERSION = "market-movement-config-v1"
PROVIDER = "binance-usdm"
EXCHANGE = "binance"
PRICE_TYPE = "trade"
WINDOWS = (1, 5, 15)
_MAX_TIMESTAMP = 9_007_199_254_740_991
_T = TypeVar("_T")


@dataclass(frozen=True)
class Metric(Generic[_T]):
    available: bool
    value: _T | None = None
    reason: str | None = None

    @classmethod
    def present(cls, value):
        return cls(True, value, None)

    @classmethod
    def missing(cls, reason):
        return cls(False, None, reason)


@dataclass(frozen=True)
class MarketMovementConfig:
    version: str = DEFAULT_CONFIG_VERSION
    historical_lookback_ms: int = 7 * 24 * 60 * 60 * 1000
    minimum_historical_coverage_ms: int = 3 * 24 * 60 * 60 * 1000
    flat_z: float = 0.5
    material_z: float = 1.0
    trim_fraction: float = 0.10
    liquidity_weight_cap: float = 0.25
    rvol_comparison_windows: int = 20
    outlier_cross_z: float = 3.5
    outlier_historical_z: float = 1.5
    minimum_eligible_fraction: float = 0.60
    minimum_eligible_count: int = 5

    def __post_init__(self):
        if not isinstance(self.version, str) or not self.version:
            raise ValueError("config version is required")
        for name in ("historical_lookback_ms", "minimum_historical_coverage_ms",
                     "rvol_comparison_windows", "minimum_eligible_count"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.minimum_historical_coverage_ms > self.historical_lookback_ms:
            raise ValueError("minimum historical coverage exceeds lookback")
        for name in ("flat_z", "material_z", "trim_fraction",
                     "liquidity_weight_cap", "outlier_cross_z",
                     "outlier_historical_z", "minimum_eligible_fraction"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if self.flat_z < 0 or self.material_z < self.flat_z:
            raise ValueError("direction thresholds are invalid")
        if not 0 < self.trim_fraction < 0.5:
            raise ValueError("trim_fraction must be between zero and one half")
        if not 0 < self.liquidity_weight_cap <= 1:
            raise ValueError("liquidity_weight_cap must be in (0, 1]")
        if self.outlier_cross_z <= 0 or self.outlier_historical_z <= 0:
            raise ValueError("outlier thresholds must be positive")
        if not 0 < self.minimum_eligible_fraction <= 1:
            raise ValueError("minimum_eligible_fraction must be in (0, 1]")


@dataclass(frozen=True)
class MarketUniverseInput:
    id: str
    version: str
    symbols: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id or not isinstance(self.version, str) or not self.version:
            raise ValueError("universe id and version are required")
        symbols = tuple(self.symbols)
        if any(not isinstance(symbol, str) or not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("universe symbols must be unique nonempty strings")
        object.__setattr__(self, "symbols", symbols)


@dataclass(frozen=True)
class HistoricalWindowInput:
    returns: tuple[float, ...]
    usable_coverage_ms: int
    previous_notional_volumes: tuple[Decimal, ...]

    def __post_init__(self):
        if type(self.usable_coverage_ms) is not int or self.usable_coverage_ms < 0:
            raise ValueError("usable_coverage_ms must be a nonnegative integer")
        object.__setattr__(self, "returns", tuple(self.returns))
        object.__setattr__(self, "previous_notional_volumes", tuple(self.previous_notional_volumes))


@dataclass(frozen=True)
class MarketMovementSymbolInput:
    symbol: str
    instrument_id: str
    instrument_compatible: bool | None
    readiness: Mapping[int, MovementReadiness]
    historical: Mapping[int, HistoricalWindowInput]

    def __post_init__(self):
        if not isinstance(self.symbol, str) or not self.symbol or not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("symbol and instrument_id are required")
        if self.instrument_compatible is not None and type(self.instrument_compatible) is not bool:
            raise ValueError("instrument_compatible must be boolean or unknown")
        if any(not isinstance(value, MovementReadiness) for value in self.readiness.values()):
            raise ValueError("readiness must contain canonical MovementReadiness values")
        if any(not isinstance(value, HistoricalWindowInput) for value in self.historical.values()):
            raise ValueError("historical must contain HistoricalWindowInput values")
        object.__setattr__(self, "readiness", MappingProxyType(dict(self.readiness)))
        object.__setattr__(self, "historical", MappingProxyType(dict(self.historical)))


@dataclass(frozen=True)
class MarketMovementInput:
    evaluation_boundary_time_ms: int
    universe: MarketUniverseInput
    symbols: Mapping[str, MarketMovementSymbolInput]
    config: MarketMovementConfig = field(default_factory=MarketMovementConfig)

    def __post_init__(self):
        if (type(self.evaluation_boundary_time_ms) is not int
                or not 0 <= self.evaluation_boundary_time_ms <= _MAX_TIMESTAMP
                or self.evaluation_boundary_time_ms % BUCKET_INTERVAL_MS):
            raise ValueError("evaluation boundary must be a nonnegative aligned safe integer")
        if not isinstance(self.universe, MarketUniverseInput) or not isinstance(self.config, MarketMovementConfig):
            raise ValueError("universe and config must have the declared types")
        if any(key != value.symbol for key, value in self.symbols.items()):
            raise ValueError("symbol input keys must match their symbols")
        object.__setattr__(self, "symbols", MappingProxyType(dict(self.symbols)))


@dataclass(frozen=True)
class ExcludedSymbol:
    symbol: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SymbolMovementResult:
    symbol: str
    instrument_id: str | None
    provider: str
    exchange: str
    price_type: str
    window_minutes: int
    evaluation_boundary_time_ms: int
    included: bool
    exclusion_reasons: tuple[str, ...]
    current_return: Metric[float]
    previous_return: Metric[float]
    velocity: Metric[float]
    previous_velocity: Metric[float]
    acceleration: Metric[float]
    historical_median: Metric[float]
    historical_mad: Metric[float]
    normalized_z: Metric[float]
    direction: Metric[str]
    material_rising: bool
    material_falling: bool
    current_notional_volume: Metric[Decimal]
    rvol: Metric[float]
    cross_sectional_z: Metric[float]
    outlier_candidate: bool


@dataclass(frozen=True)
class BreadthSide:
    count: int
    fraction: float


@dataclass(frozen=True)
class WindowBreadth:
    available: bool
    reason: str | None
    denominator: int
    flat: Metric[BreadthSide]
    rising: Metric[BreadthSide]
    falling: Metric[BreadthSide]
    material_rising: Metric[BreadthSide]
    material_falling: Metric[BreadthSide]

    @property
    def availability(self):
        return self.available


@dataclass(frozen=True)
class WindowAggregates:
    median_normalized_movement: Metric[float]
    median_raw_return: Metric[float]
    trimmed_mean_normalized_movement: Metric[float]
    liquidity_weighted_normalized_movement: Metric[float]
    liquidity_weights: Metric[tuple[tuple[str, float], ...]]
    dispersion_mad_normalized_movement: Metric[float]


@dataclass(frozen=True)
class MarketMovementWindowResult:
    algorithm_version: str
    config_version: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    included_symbols: tuple[str, ...]
    excluded_symbols: tuple[ExcludedSymbol, ...]
    window_minutes: int
    provider: str
    exchange: str
    price_type: str
    evaluation_boundary_time_ms: int
    historical_lookback_ms: int
    minimum_historical_coverage_ms: int
    market_wide_eligible: bool
    eligible_count: int
    eligible_fraction: float
    symbols: tuple[SymbolMovementResult, ...]
    breadth: WindowBreadth
    aggregates: WindowAggregates


@dataclass(frozen=True)
class MarketMovementEvaluation:
    algorithm_version: str
    config_version: str
    universe_id: str
    universe_version: str
    configured_universe: tuple[str, ...]
    provider: str
    exchange: str
    price_type: str
    evaluation_boundary_time_ms: int
    historical_lookback_ms: int
    minimum_historical_coverage_ms: int
    windows: Mapping[int, MarketMovementWindowResult]

    def __post_init__(self):
        object.__setattr__(self, "windows", MappingProxyType(dict(self.windows)))

    @property
    def window_minutes(self):
        return WINDOWS

    @property
    def included_symbols(self):
        return MappingProxyType({window: result.included_symbols
                                 for window, result in self.windows.items()})

    @property
    def excluded_symbols(self):
        return MappingProxyType({window: result.excluded_symbols
                                 for window, result in self.windows.items()})

    @property
    def market_wide_eligible(self):
        return MappingProxyType({window: result.market_wide_eligible
                                 for window, result in self.windows.items()})

    @property
    def eligible_count(self):
        return MappingProxyType({window: result.eligible_count
                                 for window, result in self.windows.items()})

    @property
    def eligible_fraction(self):
        return MappingProxyType({window: result.eligible_fraction
                                 for window, result in self.windows.items()})


_READINESS_REASONS = {
    "collector_recovering": "SOURCE_RECOVERING",
    "collector_stale": "SOURCE_STALE",
    "collector_unavailable": "SOURCE_UNAVAILABLE",
    "insufficient_exact_live_history": "WARMING_INSUFFICIENT_LIVE_HISTORY",
    "last_real_trade_expired": "STALE_LAST_TRADE",
    "boundary_not_retained_or_finalized": "MISSING_EXACT_BOUNDARY",
    "noncontiguous_live_history": "MISSING_EXACT_BOUNDARY",
    "source_unavailable_in_required_history": "SOURCE_UNAVAILABLE",
    "no_real_trade_history": "MOVEMENT_HISTORY_UNAVAILABLE",
    "unusable_price_history": "MOVEMENT_HISTORY_UNAVAILABLE",
    "no_real_trade_endpoint": "MOVEMENT_HISTORY_UNAVAILABLE",
}


def _finite_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    # Halving first avoids overflow when two finite float inputs are large.
    if isinstance(ordered[middle - 1], float) or isinstance(ordered[middle], float):
        return ordered[middle - 1] / 2 + ordered[middle] / 2
    return (ordered[middle - 1] + ordered[middle]) / 2


def _nonnegative_decimal(value):
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return None
    return value


def _log_return(end, start):
    try:
        if (not isinstance(end, Decimal) or not isinstance(start, Decimal)
                or not end.is_finite() or not start.is_finite()
                or end <= 0 or start <= 0):
            return None
        result = float(end.ln() - start.ln())
    except (InvalidOperation, OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _normalized(current, historical, config):
    absent = Metric.missing("INSUFFICIENT_NORMALIZATION_HISTORY")
    if historical is None or historical.usable_coverage_ms < config.minimum_historical_coverage_ms or not historical.returns:
        return absent, absent, absent, "INSUFFICIENT_NORMALIZATION_HISTORY"
    values = tuple(_finite_float(value) for value in historical.returns)
    if any(value is None for value in values):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return absent, absent, absent, "INVALID_NORMALIZATION_HISTORY"
    center = _median(values)
    mad = _median(abs(value - center) for value in values)
    if not math.isfinite(center) or not math.isfinite(mad):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return absent, absent, absent, "INVALID_NORMALIZATION_HISTORY"
    if mad <= 0:
        absent = Metric.missing("NORMALIZATION_MAD_UNAVAILABLE")
        return Metric.present(center), absent, absent, "NORMALIZATION_MAD_UNAVAILABLE"
    if current is None:
        return Metric.present(center), Metric.present(mad), Metric.missing("SYMBOL_EXCLUDED"), None
    z = 0.6745 * ((current - center) / mad)
    if not math.isfinite(z):
        absent = Metric.missing("INVALID_NORMALIZATION_HISTORY")
        return Metric.present(center), Metric.present(mad), absent, "INVALID_NORMALIZATION_HISTORY"
    return Metric.present(center), Metric.present(mad), Metric.present(z), None


def _notional(history, boundary, window):
    start = boundary - window * 60_000
    selected = [bucket for bucket in history if start < bucket.boundary_time_ms <= boundary]
    expected = tuple(range(start + BUCKET_INTERVAL_MS, boundary + 1, BUCKET_INTERVAL_MS))
    if tuple(bucket.boundary_time_ms for bucket in selected) != expected:
        return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
    volumes = tuple(_nonnegative_decimal(bucket.quote_volume) for bucket in selected)
    if any(volume is None for volume in volumes):
        return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
    return Metric.present(sum(volumes, Decimal(0)))


def _rvol(current_notional, historical, config):
    if not current_notional.available:
        return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
    if historical is None or len(historical.previous_notional_volumes) < config.rvol_comparison_windows:
        return Metric.missing("RVOL_HISTORY_UNAVAILABLE")
    selected = historical.previous_notional_volumes[-config.rvol_comparison_windows:]
    values = tuple(_nonnegative_decimal(value) for value in selected)
    if any(value is None for value in values):
        return Metric.missing("RVOL_HISTORY_UNAVAILABLE")
    denominator = _median(values)
    if denominator <= 0:
        return Metric.missing("RVOL_DENOMINATOR_INVALID")
    try:
        result = float(current_notional.value / denominator)
    except (InvalidOperation, OverflowError, ValueError):
        return Metric.missing("RVOL_DENOMINATOR_INVALID")
    if not math.isfinite(result):
        return Metric.missing("RVOL_DENOMINATOR_INVALID")
    return Metric.present(result)


def _symbol_result(request, symbol, window):
    supplied = request.symbols.get(symbol)
    boundary = request.evaluation_boundary_time_ms
    missing = Metric.missing("SYMBOL_EXCLUDED")
    reasons = []
    current = previous = velocity = previous_velocity = acceleration = missing
    current_notional = Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
    historical_median = historical_mad = normalized_z = missing
    direction = missing
    rvol = Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
    if supplied is None:
        reasons.append("MISSING_SYMBOL_INPUT")
    else:
        if supplied.instrument_compatible is False:
            reasons.append("UNSUPPORTED_INSTRUMENT")
        elif supplied.instrument_compatible is None:
            reasons.append("SOURCE_UNAVAILABLE")
        readiness = supplied.readiness.get(window)
        historical = supplied.historical.get(window)
        if readiness is None:
            reasons.append("MOVEMENT_HISTORY_UNAVAILABLE")
        elif readiness.state != "ready":
            reason = _READINESS_REASONS.get(readiness.reason)
            if reason is None:
                reason = {
                    "warming": "WARMING_INSUFFICIENT_LIVE_HISTORY",
                    "missing_history": "MISSING_EXACT_BOUNDARY",
                    "stale": "SOURCE_STALE",
                    "unavailable": "SOURCE_UNAVAILABLE",
                }.get(readiness.state, "MOVEMENT_HISTORY_UNAVAILABLE")
            reasons.append(reason)
        else:
            start = boundary - 2 * window * 60_000
            expected = tuple(range(start, boundary + 1, BUCKET_INTERVAL_MS))
            history = readiness.history
            if (readiness.boundary_time_ms != boundary or readiness.window_minutes != window
                    or len(history) != len(expected)
                    or tuple(bucket.boundary_time_ms for bucket in history) != expected
                    or readiness.endpoint is None
                    or readiness.endpoint.boundary_time_ms != boundary):
                reasons.append("MISSING_EXACT_BOUNDARY")
            else:
                by_boundary = {bucket.boundary_time_ms: bucket for bucket in history}
                current_value = _log_return(by_boundary[boundary].price, by_boundary[boundary - window * 60_000].price)
                previous_value = _log_return(by_boundary[boundary - window * 60_000].price, by_boundary[start].price)
                if current_value is None or previous_value is None:
                    reasons.append("INVALID_ENDPOINT_PRICE")
                else:
                    duration = window * 60
                    current = Metric.present(current_value)
                    previous = Metric.present(previous_value)
                    velocity = Metric.present(current_value / duration)
                    previous_velocity = Metric.present(previous_value / duration)
                    acceleration = Metric.present((velocity.value - previous_velocity.value) / duration)
                current_notional = _notional(history, boundary, window)
        historical_median, historical_mad, normalized_z, normalization_reason = _normalized(
            current.value if current.available else None, historical, request.config
        )
        if normalization_reason:
            reasons.append(normalization_reason)
        rvol = _rvol(current_notional, historical, request.config)
    included = not reasons
    if included:
        z = normalized_z.value
        raw = current.value
        if raw == 0 or abs(z) < request.config.flat_z:
            direction = Metric.present("FLAT")
        elif raw > 0:
            direction = Metric.present("RISING")
        else:
            direction = Metric.present("FALLING")
    return SymbolMovementResult(
        symbol=symbol, instrument_id=None if supplied is None else supplied.instrument_id,
        provider=PROVIDER, exchange=EXCHANGE, price_type=PRICE_TYPE,
        window_minutes=window, evaluation_boundary_time_ms=boundary,
        included=included, exclusion_reasons=tuple(dict.fromkeys(reasons)),
        current_return=current, previous_return=previous, velocity=velocity,
        previous_velocity=previous_velocity, acceleration=acceleration,
        historical_median=historical_median, historical_mad=historical_mad,
        normalized_z=normalized_z, direction=direction,
        material_rising=included and current.value > 0 and abs(normalized_z.value) >= request.config.material_z,
        material_falling=included and current.value < 0 and abs(normalized_z.value) >= request.config.material_z,
        current_notional_volume=current_notional, rvol=rvol,
        cross_sectional_z=Metric.missing("SYMBOL_EXCLUDED"), outlier_candidate=False,
    )


def _breadth(included, eligible):
    if not eligible:
        missing = Metric.missing("MARKET_UNIVERSE_INELIGIBLE")
        return WindowBreadth(False, "MARKET_UNIVERSE_INELIGIBLE", len(included),
                             missing, missing, missing, missing, missing)
    denominator = len(included)
    def side(predicate):
        count = sum(bool(predicate(item)) for item in included)
        return Metric.present(BreadthSide(count, count / denominator))
    return WindowBreadth(True, None, denominator,
                         side(lambda item: item.direction.value == "FLAT"),
                         side(lambda item: item.direction.value == "RISING"),
                         side(lambda item: item.direction.value == "FALLING"),
                         side(lambda item: item.material_rising),
                         side(lambda item: item.material_falling))


def _capped_weights(included, cap):
    raw = []
    for item in included:
        if not item.current_notional_volume.available:
            return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
        try:
            weight = math.sqrt(float(item.current_notional_volume.value))
        except (OverflowError, ValueError):
            return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
        if not math.isfinite(weight):
            return Metric.missing("CURRENT_NOTIONAL_UNAVAILABLE")
        raw.append(weight)
    active = {i for i, weight in enumerate(raw) if weight > 0}
    if len(active) * cap < 1:
        return Metric.missing("LIQUIDITY_WEIGHT_CAP_UNSATISFIABLE")
    weights = [0.0] * len(raw)
    remaining = 1.0
    while active:
        total_raw = sum(raw[i] for i in active)
        capped = {i for i in active if remaining * raw[i] / total_raw > cap}
        if not capped:
            for i in active:
                weights[i] = remaining * raw[i] / total_raw
            break
        for i in capped:
            weights[i] = cap
            remaining -= cap
        active -= capped
    return Metric.present(tuple((item.symbol, weights[i]) for i, item in enumerate(included)))


def _aggregates(included, eligible, config):
    if not eligible:
        missing = Metric.missing("MARKET_UNIVERSE_INELIGIBLE")
        return WindowAggregates(missing, missing, missing, missing, missing, missing)
    z_values = sorted(item.normalized_z.value for item in included)
    raw_values = [item.current_return.value for item in included]
    center = _median(z_values)
    k = math.floor(len(z_values) * config.trim_fraction)
    if k < 1 or 2 * k >= len(z_values):
        trimmed = Metric.missing("TOO_FEW_VALUES_TO_TRIM")
    else:
        try:
            trimmed_value = math.fsum(z_values[k:-k]) / (len(z_values) - 2 * k)
        except OverflowError:
            trimmed_value = math.inf
        trimmed = (Metric.present(trimmed_value) if math.isfinite(trimmed_value)
                   else Metric.missing("NUMERIC_RESULT_UNAVAILABLE"))
    weights = _capped_weights(included, config.liquidity_weight_cap)
    if weights.available:
        try:
            weighted_value = math.fsum(item.normalized_z.value * weight
                                      for item, (_, weight) in zip(included, weights.value))
        except OverflowError:
            weighted_value = math.inf
        weighted = (Metric.present(weighted_value) if math.isfinite(weighted_value)
                    else Metric.missing("NUMERIC_RESULT_UNAVAILABLE"))
    else:
        weighted = Metric.missing(weights.reason)
    dispersion = _median(abs(value - center) for value in z_values)
    return WindowAggregates(Metric.present(center), Metric.present(_median(raw_values)),
                            trimmed, weighted, weights,
                            Metric.present(dispersion) if math.isfinite(dispersion)
                            else Metric.missing("NUMERIC_RESULT_UNAVAILABLE"))


def _outliers(results, config):
    included = [item for item in results if item.included]
    if not included:
        return results
    center = _median(item.current_return.value for item in included)
    mad = _median(abs(item.current_return.value - center) for item in included)
    updated = []
    for item in results:
        if not item.included:
            updated.append(item)
        elif mad <= 0 or not math.isfinite(mad):
            updated.append(replace(item, cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE")))
        else:
            z = 0.6745 * ((item.current_return.value - center) / mad)
            if not math.isfinite(z):
                updated.append(replace(item, cross_sectional_z=Metric.missing("CROSS_SECTIONAL_MAD_UNAVAILABLE")))
            else:
                updated.append(replace(
                    item, cross_sectional_z=Metric.present(z),
                    outlier_candidate=(abs(z) >= config.outlier_cross_z
                                       and abs(item.normalized_z.value) >= config.outlier_historical_z),
                ))
    return tuple(updated)


def calculate_market_movement(request: MarketMovementInput) -> MarketMovementEvaluation:
    """Calculate all three windows from the same explicit boundary and universe."""
    if not isinstance(request, MarketMovementInput):
        raise ValueError("request must be MarketMovementInput")
    windows = {}
    for window in WINDOWS:
        results = tuple(_symbol_result(request, symbol, window) for symbol in request.universe.symbols)
        included = tuple(item for item in results if item.included)
        count = len(included)
        fraction = count / len(request.universe.symbols) if request.universe.symbols else 0.0
        eligible = (count >= request.config.minimum_eligible_count
                    and fraction >= request.config.minimum_eligible_fraction)
        if eligible:
            results = _outliers(results, request.config)
        else:
            results = tuple(replace(
                item,
                cross_sectional_z=Metric.missing("MARKET_UNIVERSE_INELIGIBLE"),
                outlier_candidate=False,
            ) for item in results)
        windows[window] = MarketMovementWindowResult(
            algorithm_version=ALGORITHM_VERSION, config_version=request.config.version,
            universe_id=request.universe.id, universe_version=request.universe.version,
            configured_universe=request.universe.symbols,
            included_symbols=tuple(item.symbol for item in included),
            excluded_symbols=tuple(ExcludedSymbol(item.symbol, item.exclusion_reasons)
                                   for item in results if not item.included),
            window_minutes=window, provider=PROVIDER, exchange=EXCHANGE, price_type=PRICE_TYPE,
            evaluation_boundary_time_ms=request.evaluation_boundary_time_ms,
            historical_lookback_ms=request.config.historical_lookback_ms,
            minimum_historical_coverage_ms=request.config.minimum_historical_coverage_ms,
            market_wide_eligible=eligible, eligible_count=count, eligible_fraction=fraction,
            symbols=results, breadth=_breadth(included, eligible),
            aggregates=_aggregates(included, eligible, request.config),
        )
    return MarketMovementEvaluation(
        ALGORITHM_VERSION, request.config.version, request.universe.id, request.universe.version,
        request.universe.symbols, PROVIDER, EXCHANGE, PRICE_TYPE,
        request.evaluation_boundary_time_ms, request.config.historical_lookback_ms,
        request.config.minimum_historical_coverage_ms, windows,
    )
