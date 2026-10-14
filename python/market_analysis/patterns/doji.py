"""Frozen doji-v1 definition and complete, descriptive 0-100 score records."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..technical import _finite
from .base import PatternComponent, PatternReading, PatternTier
from .features import CandleFeatures


@dataclass(frozen=True)
class DojiParams:
    pattern_id: str = field(default="doji", init=False)
    version: str = field(default="doji-v1", init=False)
    atr_period: int = 14
    adx_period: int = 14
    ema_fast_period: int = 20
    ema_slow_period: int = 50
    lookback: int = 20
    body_max: float = 0.10
    range_atr_min: float = 0.5
    size_span: float = 1.0
    volume_floor: float = 0.5
    volume_span: float = 1.5
    adx_min: float = 20.0
    adx_span: float = 20.0
    dominant_wick_min: float = 0.60
    opposite_wick_max: float = 0.10
    long_wick_min: float = 0.30
    tightness_weight: float = 0.20
    size_weight: float = 0.25
    volume_weight: float = 0.20
    trend_weight: float = 0.20
    location_weight: float = 0.15
    strong_min: float = 50.0
    very_strong_min: float = 75.0


DOJI_PARAMS = DojiParams()


def score_tier(score: float) -> PatternTier:
    if score >= DOJI_PARAMS.very_strong_min:
        return "very_strong"
    if score >= DOJI_PARAMS.strong_min:
        return "strong"
    return "weak"


def _empty(status, reasons) -> PatternReading:
    return PatternReading(DOJI_PARAMS.pattern_id, DOJI_PARAMS.version, status,
                          None, None, None, (), tuple(reasons))


def read_doji(features: CandleFeatures) -> PatternReading:
    """No partial scores. Doji direction is supplied only by strategy wrappers."""
    p = DOJI_PARAMS
    if features.range == 0:
        return _empty("insufficient", ("zero_range",))
    if not features.ready:
        return _empty("insufficient", features.missing_inputs or ("prior_input_unavailable",))
    if features.atr_prev <= 0:
        return _empty("not_pattern", ("atr_nonpositive",))
    if not all(_finite(value) for value in (
            features.range, features.body_ratio, features.upper_ratio,
            features.lower_ratio, features.range_atr)):
        return _empty("insufficient", ("geometry_unavailable",))
    failures = tuple(reason for failed, reason in (
        (features.range <= 0, "range_nonpositive"),
        (features.body_ratio > p.body_max, "body_too_large"),
        (features.range_atr < p.range_atr_min, "range_too_small"),
    ) if failed)
    if failures:
        return _empty("not_pattern", failures)
    if features.lower_ratio >= p.dominant_wick_min and features.upper_ratio <= p.opposite_wick_max:
        subtype = "dragonfly"
    elif features.upper_ratio >= p.dominant_wick_min and features.lower_ratio <= p.opposite_wick_max:
        subtype = "gravestone"
    elif features.upper_ratio >= p.long_wick_min and features.lower_ratio >= p.long_wick_min:
        subtype = "long_legged"
    else:
        subtype = "standard"
    if features.trend_dir > 0:
        distance = max(0.0, features.prior20_high - features.high) / features.atr_prev
    elif features.trend_dir < 0:
        distance = max(0.0, features.low - features.prior20_low) / features.atr_prev
    else:
        distance = None
    values = (
        ("tightness", features.body_ratio, 1 - features.body_ratio / p.body_max, p.tightness_weight),
        ("size", features.range_atr, (features.range_atr - p.range_atr_min) / p.size_span, p.size_weight),
        ("volume", features.rel_volume, (features.rel_volume - p.volume_floor) / p.volume_span, p.volume_weight),
        ("trend", features.adx_prev, (features.adx_prev - p.adx_min) / p.adx_span, p.trend_weight),
        ("location", distance, 1 - distance if distance is not None else 0.0, p.location_weight),
    )
    components = tuple(PatternComponent(name, raw, min(1.0, max(0.0, normalized)), weight,
                                       100 * weight * min(1.0, max(0.0, normalized)))
                       for name, raw, normalized, weight in values)
    score = sum(component.contribution for component in components)
    return PatternReading(p.pattern_id, p.version, "ok", subtype, score,
                          score_tier(score), components, ("gate_passed", f"subtype:{subtype}"))


@dataclass(frozen=True)
class DojiPattern:
    """Implements CandlePattern without a global pattern registry."""

    @property
    def pattern_id(self) -> str:
        return DOJI_PARAMS.pattern_id

    @property
    def version(self) -> str:
        return DOJI_PARAMS.version

    def read(self, features: CandleFeatures) -> PatternReading:
        return read_doji(features)
