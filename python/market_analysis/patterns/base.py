"""Small immutable contract shared by independently implemented patterns."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .features import CandleFeatures

PatternStatus = Literal["ok", "insufficient", "not_pattern"]
PatternTier = Literal["weak", "strong", "very_strong"]


@dataclass(frozen=True)
class PatternComponent:
    name: str
    raw: float | None
    normalized: float
    weight: float
    contribution: float  # Score points: 100 * weight * normalized.


@dataclass(frozen=True)
class PatternReading:
    pattern_id: str
    version: str
    status: PatternStatus
    subtype: str | None
    score: float | None
    tier: PatternTier | None
    components: tuple[PatternComponent, ...]
    reasons: tuple[str, ...]


class CandlePattern(Protocol):
    """A second pattern implements read() in its own module; no registration needed."""

    @property
    def pattern_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def read(self, features: CandleFeatures) -> PatternReading: ...
