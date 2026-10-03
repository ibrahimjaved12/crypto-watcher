"""Pure deterministic #73 lifecycle over canonical #72 classifications.

All time and evidence arrive in the classification or explicit prior state. This
module neither acquires market data nor persists its results.
"""

from dataclasses import dataclass, fields, replace
from decimal import Decimal
import math

from .canonical_identity import canonical_digest as _digest, canonical_value as _canonical

from .movement_classifier import (
    IsolatedOutlier, MarketClassificationEvaluation, MarketWindowClassification,
    SymbolSourceTimeEvidence, SymbolVolumeContext,
)
from .movement_metrics import BreadthSide, ExcludedSymbol, Metric, SymbolMovementResult


ALGORITHM_VERSION = "market-episode-lifecycle-v1"
DEFAULT_CONFIG_VERSION = "market-episode-lifecycle-config-v1"
STATE_SERIALIZATION_VERSION = "market-episode-state-v1"
EVENT_FAMILY = "BROAD_MOVE"
EVALUATION_CADENCE_MS = 5_000
_V1_RULE_VALUES = {
    "evaluation_cadence_ms": 5_000,
    "start_confirmation_count": 2,
    "end_confirmation_count": 3,
    "reversal_confirmation_count": 2,
    "strengthen_confirmation_count": 2,
    "weaken_confirmation_count": 2,
    "resume_confirmation_count": 2,
    "continuation_breadth": 0.55,
    "material_strengthen_breadth": 0.70,
    "material_weaken_breadth": 0.50,
}
_BROAD = frozenset(("BROAD_RISE", "BROAD_DROP"))
_DIRECTIONS = _BROAD | frozenset(("NEUTRAL", "WARMING", "UNAVAILABLE"))
_PACES = frozenset(("ACCELERATING", "DECELERATING", "MIXED"))
_STRENGTH_REASONS = ("pace_accelerating", "material_strengthening")
_WEAKNESS_REASONS = ("pace_decelerating", "material_weakening")


def _boundary(value):
    if type(value) is not int or value < 0 or value % EVALUATION_CADENCE_MS:
        raise ValueError("evaluation boundary must be a nonnegative aligned integer")


def _nonempty(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class MarketEpisodeLifecycleConfig:
    version: str = DEFAULT_CONFIG_VERSION
    evaluation_cadence_ms: int = EVALUATION_CADENCE_MS
    start_confirmation_count: int = 2
    end_confirmation_count: int = 3
    reversal_confirmation_count: int = 2
    strengthen_confirmation_count: int = 2
    weaken_confirmation_count: int = 2
    resume_confirmation_count: int = 2
    continuation_breadth: float = 0.55
    material_strengthen_breadth: float = 0.70
    material_weaken_breadth: float = 0.50

    def __post_init__(self):
        _nonempty(self.version, "lifecycle config version")
        for name in ("start_confirmation_count", "end_confirmation_count",
                     "reversal_confirmation_count", "strengthen_confirmation_count",
                     "weaken_confirmation_count", "resume_confirmation_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("continuation_breadth", "material_strengthen_breadth",
                     "material_weaken_breadth"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 1):
                raise ValueError(f"{name} must be finite and in (0, 1]")
        if self.material_weaken_breadth >= self.material_strengthen_breadth:
            raise ValueError("material weakening must be below strengthening")
        for name, expected in _V1_RULE_VALUES.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} is fixed by {ALGORITHM_VERSION} at {expected}")


@dataclass(frozen=True)
class MarketEpisodeScope:
    lifecycle_algorithm_version: str
    lifecycle_config_version: str
    universe_id: str
    universe_version: str
    primary_window_minutes: int
    classifier_algorithm_version: str
    classifier_config_version: str
    movement_algorithm_version: str
    movement_config_version: str
    provider: str
    exchange: str
    price_type: str

    def __post_init__(self):
        for field in fields(self):
            if field.name != "primary_window_minutes":
                _nonempty(getattr(self, field.name), field.name)
        if self.primary_window_minutes not in (1, 5, 15):
            raise ValueError("episode primary window is invalid")


@dataclass(frozen=True)
class ActiveMarketEpisode:
    episode_id: str
    event_family: str
    direction: str
    scope: MarketEpisodeScope
    start_boundary_time_ms: int
    last_observed_evaluation_boundary_time_ms: int

    def __post_init__(self):
        _nonempty(self.episode_id, "episode id")
        if (self.event_family != EVENT_FAMILY or not isinstance(self.direction, str)
                or self.direction not in _BROAD):
            raise ValueError("active episode family or direction is invalid")
        if not isinstance(self.scope, MarketEpisodeScope):
            raise ValueError("active episode scope is invalid")
        _boundary(self.start_boundary_time_ms)
        _boundary(self.last_observed_evaluation_boundary_time_ms)
        if self.start_boundary_time_ms > self.last_observed_evaluation_boundary_time_ms:
            raise ValueError("episode start exceeds last observation")
        if (self.scope.lifecycle_algorithm_version == ALGORITHM_VERSION
                and self.episode_id != _episode_id(self.scope, self.direction,
                                                   self.start_boundary_time_ms)):
            raise ValueError("episode id does not match its canonical identity")


@dataclass(frozen=True)
class PendingDirection:
    direction: str
    count: int
    start_boundary_time_ms: int

    def __post_init__(self):
        if (not isinstance(self.direction, str) or self.direction not in _BROAD
                or type(self.count) is not int or self.count < 1):
            raise ValueError("pending direction or count is invalid")
        _boundary(self.start_boundary_time_ms)


@dataclass(frozen=True)
class PendingCrossing:
    reason: str
    count: int

    def __post_init__(self):
        if self.reason not in _STRENGTH_REASONS + _WEAKNESS_REASONS:
            raise ValueError("pending crossing reason is invalid")
        if type(self.count) is not int or self.count < 1:
            raise ValueError("pending crossing count is invalid")


@dataclass(frozen=True)
class MarketEpisodeLifecycleState:
    lifecycle_algorithm_version: str
    lifecycle_config_version: str
    lifecycle_config: MarketEpisodeLifecycleConfig
    scope: MarketEpisodeScope
    last_evaluation_boundary_time_ms: int
    last_classification_fingerprint: str
    current_direction_state: str
    current_pace: Metric[str]
    active_episode: ActiveMarketEpisode | None = None
    pending_start: PendingDirection | None = None
    pending_reversal: PendingDirection | None = None
    continuation_failure_count: int = 0
    pending_strengthen: tuple[PendingCrossing, ...] = ()
    pending_weaken: tuple[PendingCrossing, ...] = ()
    interrupted: bool = False
    pending_resume: PendingDirection | None = None
    previous_usable_pace: str | None = None
    previous_same_direction_material_breadth: float | None = None

    def __post_init__(self):
        _nonempty(self.lifecycle_algorithm_version, "lifecycle algorithm version")
        if not isinstance(self.lifecycle_config, MarketEpisodeLifecycleConfig):
            raise ValueError("lifecycle config is invalid")
        if not isinstance(self.scope, MarketEpisodeScope):
            raise ValueError("lifecycle scope is invalid")
        if (self.lifecycle_config_version != self.lifecycle_config.version
                or self.scope.lifecycle_config_version != self.lifecycle_config_version
                or self.scope.lifecycle_algorithm_version != self.lifecycle_algorithm_version):
            raise ValueError("lifecycle state scope/config versions disagree")
        _boundary(self.last_evaluation_boundary_time_ms)
        fingerprint = self.last_classification_fingerprint
        if (not isinstance(fingerprint, str) or len(fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in fingerprint)):
            raise ValueError("classification fingerprint is invalid")
        if (not isinstance(self.current_direction_state, str)
                or self.current_direction_state not in _DIRECTIONS):
            raise ValueError("current direction is invalid")
        if not isinstance(self.current_pace, Metric):
            raise ValueError("current pace metric is invalid")
        if type(self.current_pace.available) is not bool:
            raise ValueError("pace availability is invalid")
        if (self.current_pace.available and
                (not isinstance(self.current_pace.value, str)
                 or self.current_pace.value not in _PACES)):
            raise ValueError("current pace is invalid")
        if self.current_pace.available and self.current_pace.reason is not None:
            raise ValueError("available pace cannot have an unavailable reason")
        if (not self.current_pace.available and
                (self.current_pace.value is not None or
                 not isinstance(self.current_pace.reason, str) or
                 not self.current_pace.reason)):
            raise ValueError("unavailable pace requires a reason and null value")
        if type(self.continuation_failure_count) is not int or not 0 <= self.continuation_failure_count < self.lifecycle_config.end_confirmation_count:
            raise ValueError("continuation failure count is invalid")
        if type(self.interrupted) is not bool:
            raise ValueError("interrupted flag is invalid")
        for name, limit in (("pending_start", self.lifecycle_config.start_confirmation_count),
                            ("pending_reversal", self.lifecycle_config.reversal_confirmation_count),
                            ("pending_resume", self.lifecycle_config.resume_confirmation_count)):
            candidate = getattr(self, name)
            if candidate is not None and (not isinstance(candidate, PendingDirection)
                                          or candidate.count >= limit
                                          or candidate.start_boundary_time_ms >
                                          self.last_evaluation_boundary_time_ms):
                raise ValueError(f"{name} is invalid")
        for name, allowed, limit in (
            ("pending_strengthen", _STRENGTH_REASONS, self.lifecycle_config.strengthen_confirmation_count),
            ("pending_weaken", _WEAKNESS_REASONS, self.lifecycle_config.weaken_confirmation_count),
        ):
            values = getattr(self, name)
            if (not isinstance(values, tuple) or
                    len({item.reason for item in values if isinstance(item, PendingCrossing)}) != len(values)
                    or any(not isinstance(item, PendingCrossing) or item.reason not in allowed
                           or item.count >= limit for item in values)):
                raise ValueError(f"{name} is invalid")
        if (self.previous_usable_pace is not None and
                (not isinstance(self.previous_usable_pace, str)
                 or self.previous_usable_pace not in _PACES)):
            raise ValueError("previous usable pace is invalid")
        material = self.previous_same_direction_material_breadth
        if material is not None and (type(material) not in (int, float) or
                                     not math.isfinite(material) or not 0 <= material <= 1):
            raise ValueError("previous material breadth is invalid")
        if self.active_episode is None:
            if (self.pending_reversal or self.pending_resume or self.interrupted or
                    self.continuation_failure_count or self.pending_strengthen or self.pending_weaken or
                    self.previous_usable_pace is not None or material is not None):
                raise ValueError("inactive state has active-episode confirmation data")
        else:
            if (not isinstance(self.active_episode, ActiveMarketEpisode)
                    or self.active_episode.scope != self.scope
                    or self.active_episode.last_observed_evaluation_boundary_time_ms !=
                    self.last_evaluation_boundary_time_ms or self.pending_start):
                raise ValueError("active episode is inconsistent with state scope")
            if self.pending_reversal and self.pending_reversal.direction == self.active_episode.direction:
                raise ValueError("reversal must oppose the active direction")
            if self.pending_resume and (not self.interrupted or
                                        self.pending_resume.direction != self.active_episode.direction):
                raise ValueError("resume must match an interrupted episode")
            if self.interrupted and (self.pending_strengthen or self.pending_weaken or
                                     self.continuation_failure_count):
                raise ValueError("interrupted state has active confirmation counters")


@dataclass(frozen=True)
class MarketEpisodeTransition:
    event_id: str
    episode_id: str
    previous_episode_id: str | None
    transition: str
    transition_reason: str
    from_direction: str | None
    to_direction: str | None
    event_family: str
    episode_direction: str
    episode_start_boundary_time_ms: int
    evaluation_boundary_time_ms: int
    episode_scope: MarketEpisodeScope
    evaluation_scope: MarketEpisodeScope
    episode_lifecycle_config: MarketEpisodeLifecycleConfig
    evaluation_lifecycle_config: MarketEpisodeLifecycleConfig
    windows_context: tuple[MarketWindowClassification, ...]
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    directional_breadth: Metric[BreadthSide]
    material_breadth: Metric[BreadthSide]
    median_raw_return: Metric[float]
    median_normalized_movement: Metric[float]
    median_acceleration: Metric[float]
    acceleration_breadth: Metric[BreadthSide]
    pace: Metric[str]
    dispersion_mad_normalized_movement: Metric[float]
    volume_context: tuple[SymbolVolumeContext, ...]
    isolated_outliers: tuple[IsolatedOutlier, ...]
    supporting_contracts: tuple[SymbolMovementResult, ...]
    conflicting_contracts: tuple[SymbolMovementResult, ...]
    configured_universe: tuple[str, ...]
    included_symbols: tuple[str, ...]
    excluded_symbols: tuple[ExcludedSymbol, ...]
    classification: MarketClassificationEvaluation


@dataclass(frozen=True)
class MarketEpisodeLifecycleResult:
    next_state: MarketEpisodeLifecycleState
    transitions: tuple[MarketEpisodeTransition, ...]


def _scope(classification, config):
    return MarketEpisodeScope(
        ALGORITHM_VERSION, config.version, classification.universe_id,
        classification.universe_version, classification.primary_window_minutes,
        classification.classifier_algorithm_version, classification.classifier_config_version,
        classification.movement_algorithm_version, classification.movement_config_version,
        classification.provider, classification.exchange, classification.price_type,
    )


def _scope_change(old, new, old_config, new_config):
    if (old.universe_id, old.universe_version) != (new.universe_id, new.universe_version):
        return "universe_changed"
    checks = (
        ("primary_window_minutes", "primary_window_changed"),
        ("classifier_algorithm_version", "classifier_algorithm_changed"),
        ("classifier_config_version", "classifier_config_changed"),
        ("movement_algorithm_version", "movement_algorithm_changed"),
        ("movement_config_version", "movement_config_changed"),
        ("provider", "provider_changed"), ("exchange", "exchange_changed"),
        ("price_type", "price_type_changed"),
        ("lifecycle_algorithm_version", "lifecycle_algorithm_changed"),
        ("lifecycle_config_version", "lifecycle_config_changed"),
    )
    for name, reason in checks:
        if getattr(old, name) != getattr(new, name):
            return reason
    if old_config != new_config:
        return "lifecycle_config_changed"
    return None


def _episode_id(scope, direction, start):
    return f"market-episode-v1:{_digest({'scope': scope, 'family': EVENT_FAMILY, 'direction': direction, 'start_boundary_time_ms': start})}"


def _new_episode(scope, direction, start, boundary):
    return ActiveMarketEpisode(_episode_id(scope, direction, start), EVENT_FAMILY,
                               direction, scope, start, boundary)


def _event(episode, previous_episode_id, transition, reason, from_direction,
           to_direction, classification, evaluation_scope, episode_config, evaluation_config):
    primary = classification.windows[5]
    direction = episode.direction
    rising = direction == "BROAD_RISE"
    expected = "RISING" if rising else "FALLING"
    conflicting = "FALLING" if rising else "RISING"
    contracts = primary.movement_snapshot.symbols
    event_id = "market-movement-event-v1:" + _digest({
        "episode_id": episode.episode_id,
        "previous_episode_id": previous_episode_id,
        "transition": transition, "reason": reason,
        "evaluation_boundary_time_ms": classification.evaluation_boundary_time_ms,
        "from_direction": from_direction, "to_direction": to_direction,
        "evaluation_scope": evaluation_scope,
        "episode_lifecycle_config": episode_config,
        "evaluation_lifecycle_config": evaluation_config,
        "classification_fingerprint": _digest(classification),
    })
    return MarketEpisodeTransition(
        event_id, episode.episode_id, previous_episode_id, transition, reason,
        from_direction, to_direction, episode.event_family, direction,
        episode.start_boundary_time_ms,
        classification.evaluation_boundary_time_ms, episode.scope, evaluation_scope,
        episode_config, evaluation_config,
        tuple(classification.windows[minute] for minute in (1, 5, 15)),
        primary.source_time_evidence,
        primary.breadth.rising if rising else primary.breadth.falling,
        primary.breadth.material_rising if rising else primary.breadth.material_falling,
        primary.median_raw_return, primary.median_normalized_movement,
        primary.median_acceleration,
        primary.positive_acceleration_breadth if rising else primary.negative_acceleration_breadth,
        primary.pace, primary.dispersion_mad_normalized_movement,
        primary.volume_context, primary.isolated_outliers,
        tuple(item for item in contracts if item.included and item.direction.available
              and item.direction.value == expected),
        tuple(item for item in contracts if item.included and item.direction.available
              and item.direction.value == conflicting),
        primary.configured_universe, primary.included_symbols, primary.excluded_symbols,
        classification,
    )


def _pace(primary):
    return primary.pace.value if primary.pace.available else None


def _material(primary, direction):
    side = primary.breadth.material_rising if direction == "BROAD_RISE" else primary.breadth.material_falling
    return side.value.fraction if side.available else None


def _continuation(primary, direction, config):
    side = primary.breadth.rising if direction == "BROAD_RISE" else primary.breadth.falling
    raw = primary.median_raw_return
    if not primary.breadth.available or not side.available or not raw.available:
        return None
    return (side.value.fraction >= config.continuation_breadth and
            (raw.value > 0 if direction == "BROAD_RISE" else raw.value < 0))


def _advance_direction(pending, direction, boundary):
    if pending is not None and pending.direction == direction:
        return replace(pending, count=pending.count + 1)
    return PendingDirection(direction, 1, boundary)


def _advance_crossings(pending, conditions, crossings, count_required):
    held = {item.reason: item for item in pending}
    next_pending = []
    confirmed = []
    for reason in conditions:
        if not conditions[reason]:
            continue
        if reason in held:
            count = held[reason].count + 1
            if count >= count_required:
                confirmed.append(reason)
            else:
                next_pending.append(PendingCrossing(reason, count))
        elif crossings[reason]:
            if count_required == 1:
                confirmed.append(reason)
            else:
                next_pending.append(PendingCrossing(reason, 1))
    return tuple(next_pending), tuple(confirmed)


def _fresh_state(scope, config, boundary, fingerprint, primary):
    return MarketEpisodeLifecycleState(
        ALGORITHM_VERSION, config.version, config, scope, boundary, fingerprint,
        primary.direction_state, primary.pace,
    )


def _baseline(state, primary, episode):
    return replace(state, active_episode=episode, pending_start=None,
                   pending_reversal=None, continuation_failure_count=0,
                   pending_strengthen=(), pending_weaken=(), interrupted=False,
                   pending_resume=None, previous_usable_pace=_pace(primary),
                   previous_same_direction_material_breadth=_material(primary, episode.direction))


def interrupt_market_episode_state_on_restart(
    state: MarketEpisodeLifecycleState,
) -> MarketEpisodeLifecycleState:
    """Expose WARMING while retaining any episode for fresh confirmation."""
    if not isinstance(state, MarketEpisodeLifecycleState):
        raise ValueError("state must be a MarketEpisodeLifecycleState")
    return replace(state, current_direction_state="WARMING",
                   current_pace=Metric.missing("NO_BROAD_DIRECTION"),
                   pending_start=None, pending_reversal=None,
                   continuation_failure_count=0, pending_strengthen=(), pending_weaken=(),
                   interrupted=state.active_episode is not None, pending_resume=None,
                   previous_usable_pace=None,
                   previous_same_direction_material_breadth=None)


def process_market_episode_lifecycle(
    classification: MarketClassificationEvaluation,
    previous_state: MarketEpisodeLifecycleState | None = None,
    config: MarketEpisodeLifecycleConfig | None = None,
) -> MarketEpisodeLifecycleResult:
    """Apply one ordered #72 evaluation and return immutable state and transitions."""
    if not isinstance(classification, MarketClassificationEvaluation):
        raise ValueError("classification must be a MarketClassificationEvaluation")
    if previous_state is not None and not isinstance(previous_state, MarketEpisodeLifecycleState):
        raise ValueError("previous state must be a MarketEpisodeLifecycleState or None")
    config = MarketEpisodeLifecycleConfig() if config is None else config
    if not isinstance(config, MarketEpisodeLifecycleConfig):
        raise ValueError("config must be a MarketEpisodeLifecycleConfig")
    boundary = classification.evaluation_boundary_time_ms
    _boundary(boundary)
    if classification.primary_window_minutes != 5 or set(classification.windows) != {1, 5, 15}:
        raise ValueError("classification requires 1m, 5m, and 15m with a 5m primary")
    if (classification.classifier_config.version != classification.classifier_config_version or
            classification.classifier_algorithm_version !=
            classification.windows[5].classifier_algorithm_version):
        raise ValueError("classification config identity disagrees with primary window")
    for minute in (1, 5, 15):
        window = classification.windows[minute]
        if (window.window_minutes != minute or window.evaluation_boundary_time_ms != boundary or
                window.is_primary != (minute == 5) or
                window.horizon_role != {1: "RAPID", 5: "PRIMARY", 15: "PERSISTENCE"}[minute] or
                window.universe_id != classification.universe_id or
                window.universe_version != classification.universe_version or
                window.classifier_algorithm_version != classification.classifier_algorithm_version or
                window.classifier_config_version != classification.classifier_config_version or
                window.movement_algorithm_version != classification.movement_algorithm_version or
                window.movement_config_version != classification.movement_config_version or
                window.provider != classification.provider or window.exchange != classification.exchange or
                window.price_type != classification.price_type):
            raise ValueError("classification windows disagree with evaluation identity")
    primary = classification.windows[5]
    if primary.direction_state not in _DIRECTIONS:
        raise ValueError("primary direction state is invalid")
    fingerprint = _digest(classification)
    scope = _scope(classification, config)
    transitions = []
    if previous_state is not None:
        old_boundary = previous_state.last_evaluation_boundary_time_ms
        if boundary < old_boundary:
            raise ValueError("classification boundary moved backward")
        if boundary == old_boundary:
            if (fingerprint != previous_state.last_classification_fingerprint or
                    config != previous_state.lifecycle_config):
                raise ValueError("same boundary has different canonical classification or config")
            return MarketEpisodeLifecycleResult(previous_state, ())
        change_reason = _scope_change(previous_state.scope, scope,
                                      previous_state.lifecycle_config, config)
        if change_reason is not None:
            if previous_state.active_episode is not None:
                old_episode = previous_state.active_episode
                transitions.append(_event(old_episode, None, "ENDED", change_reason,
                                          old_episode.direction, None, classification, scope,
                                          previous_state.lifecycle_config, config))
            state = _fresh_state(scope, config, boundary, fingerprint, primary)
        else:
            episode = previous_state.active_episode
            if episode is not None:
                episode = replace(episode, last_observed_evaluation_boundary_time_ms=boundary)
            state = replace(previous_state, last_evaluation_boundary_time_ms=boundary,
                            last_classification_fingerprint=fingerprint,
                            current_direction_state=primary.direction_state,
                            current_pace=primary.pace, active_episode=episode)
            if boundary > old_boundary + EVALUATION_CADENCE_MS:
                state = replace(state, pending_start=None, pending_reversal=None,
                                continuation_failure_count=0, pending_strengthen=(),
                                pending_weaken=(), pending_resume=None,
                                interrupted=episode is not None)
    else:
        state = _fresh_state(scope, config, boundary, fingerprint, primary)

    direction = primary.direction_state
    episode = state.active_episode
    if direction in ("WARMING", "UNAVAILABLE"):
        state = replace(state, pending_start=None, pending_reversal=None,
                        continuation_failure_count=0, pending_strengthen=(),
                        pending_weaken=(), pending_resume=None,
                        interrupted=episode is not None)
        return MarketEpisodeLifecycleResult(state, tuple(transitions))

    if episode is None:
        if direction in _BROAD:
            candidate = _advance_direction(state.pending_start, direction, boundary)
            if candidate.count >= config.start_confirmation_count:
                episode = _new_episode(scope, direction, candidate.start_boundary_time_ms, boundary)
                state = _baseline(state, primary, episode)
                transitions.append(_event(episode, None, "STARTED", "confirmed_broad_entry",
                                          None, direction, classification, scope, config, config))
            else:
                state = replace(state, pending_start=candidate)
        else:
            state = replace(state, pending_start=None)
        return MarketEpisodeLifecycleResult(state, tuple(transitions))

    if direction in _BROAD and direction != episode.direction:
        candidate = _advance_direction(state.pending_reversal, direction, boundary)
        failures = (0 if state.interrupted else
                    state.continuation_failure_count + 1)
        if candidate.count >= config.reversal_confirmation_count:
            replacement = _new_episode(scope, direction, candidate.start_boundary_time_ms, boundary)
            state = _baseline(state, primary, replacement)
            transitions.append(_event(replacement, episode.episode_id, "REVERSED",
                                      "confirmed_opposite_broad_entry", episode.direction,
                                      direction, classification, scope, config, config))
        elif failures >= config.end_confirmation_count:
            transitions.append(_event(episode, None, "ENDED", "continuation_failed",
                                      episode.direction, None, classification, scope,
                                      config, config))
            state = replace(state, active_episode=None, pending_start=
                            PendingDirection(direction, 1, boundary),
                            pending_reversal=None, continuation_failure_count=0,
                            pending_strengthen=(), pending_weaken=(),
                            previous_usable_pace=None,
                            previous_same_direction_material_breadth=None,
                            interrupted=False, pending_resume=None)
        else:
            state = replace(state, pending_reversal=candidate, pending_resume=None,
                            pending_strengthen=(), pending_weaken=(),
                            previous_usable_pace=None,
                            previous_same_direction_material_breadth=None,
                            continuation_failure_count=failures)
        return MarketEpisodeLifecycleResult(state, tuple(transitions))

    state = replace(state, pending_reversal=None)
    if state.interrupted:
        if direction == episode.direction:
            candidate = _advance_direction(state.pending_resume, direction, boundary)
            if candidate.count >= config.resume_confirmation_count:
                state = _baseline(state, primary, episode)
            else:
                state = replace(state, pending_resume=candidate)
        else:
            state = replace(state, pending_resume=None)
        return MarketEpisodeLifecycleResult(state, tuple(transitions))

    continuation = _continuation(primary, episode.direction, config)
    if continuation is None:
        state = replace(state, interrupted=True, continuation_failure_count=0,
                        pending_strengthen=(), pending_weaken=(), pending_resume=None)
        return MarketEpisodeLifecycleResult(state, tuple(transitions))
    if not continuation:
        failures = state.continuation_failure_count + 1
        if failures >= config.end_confirmation_count:
            transitions.append(_event(episode, None, "ENDED", "continuation_failed",
                                      episode.direction, None, classification, scope,
                                      config, config))
            state = replace(state, active_episode=None, continuation_failure_count=0,
                            pending_strengthen=(), pending_weaken=(),
                            previous_usable_pace=None,
                            previous_same_direction_material_breadth=None)
        else:
            state = replace(state, continuation_failure_count=failures,
                            pending_strengthen=(), pending_weaken=(),
                            previous_usable_pace=_pace(primary) or state.previous_usable_pace,
                            previous_same_direction_material_breadth=(
                                _material(primary, episode.direction)))
        return MarketEpisodeLifecycleResult(state, tuple(transitions))

    pace = _pace(primary)
    material = _material(primary, episode.direction)
    prior_pace = state.previous_usable_pace
    prior_material = state.previous_same_direction_material_breadth
    strengthen_conditions = {
        "pace_accelerating": pace == "ACCELERATING",
        "material_strengthening": material is not None and
            material >= config.material_strengthen_breadth,
    }
    strengthen_crossings = {
        "pace_accelerating": prior_pace in ("MIXED", "DECELERATING") and
            pace == "ACCELERATING",
        "material_strengthening": prior_material is not None and material is not None and
            prior_material < config.material_strengthen_breadth <= material,
    }
    weaken_conditions = {
        "pace_decelerating": pace == "DECELERATING",
        "material_weakening": material is not None and material < config.material_weaken_breadth,
    }
    weaken_crossings = {
        "pace_decelerating": prior_pace in ("ACCELERATING", "MIXED") and
            pace == "DECELERATING",
        "material_weakening": prior_material is not None and material is not None and
            prior_material >= config.material_weaken_breadth > material,
    }
    strengthen, strengthened = _advance_crossings(
        state.pending_strengthen, strengthen_conditions, strengthen_crossings,
        config.strengthen_confirmation_count)
    weaken, weakened = _advance_crossings(
        state.pending_weaken, weaken_conditions, weaken_crossings,
        config.weaken_confirmation_count)
    state = replace(state, continuation_failure_count=0,
                    pending_strengthen=strengthen, pending_weaken=weaken,
                    previous_usable_pace=pace or prior_pace,
                    previous_same_direction_material_breadth=(
                        material if material is not None else prior_material))
    if strengthened:
        transitions.append(_event(episode, None, "STRENGTHENED", "+".join(strengthened),
                                  episode.direction, episode.direction, classification, scope,
                                  config, config))
    if weakened:
        transitions.append(_event(episode, None, "WEAKENED", "+".join(weakened),
                                  episode.direction, episode.direction, classification, scope,
                                  config, config))
    return MarketEpisodeLifecycleResult(state, tuple(transitions))


def serialize_market_episode_transition(transition: MarketEpisodeTransition) -> dict:
    """Return the complete lossless JSON-compatible evidence for one transition."""
    if not isinstance(transition, MarketEpisodeTransition):
        raise ValueError("transition must be a MarketEpisodeTransition")
    return _canonical(transition)


def serialize_market_episode_lifecycle_state(state: MarketEpisodeLifecycleState) -> dict:
    """Return a strict, JSON-compatible snapshot for deterministic continuation."""
    if not isinstance(state, MarketEpisodeLifecycleState):
        raise ValueError("state must be a MarketEpisodeLifecycleState")
    return {"serialization_version": STATE_SERIALIZATION_VERSION, **_canonical(state)}


def _strict(payload, model):
    if not isinstance(payload, dict) or set(payload) != {field.name for field in fields(model)}:
        raise ValueError(f"malformed serialized {model.__name__}")
    return payload


def deserialize_market_episode_lifecycle_state(payload: dict) -> MarketEpisodeLifecycleState:
    """Reject malformed or inconsistent state rather than inferring missing fields."""
    if not isinstance(payload, dict) or payload.get("serialization_version") != STATE_SERIALIZATION_VERSION:
        raise ValueError("unsupported market episode state serialization version")
    body = {key: value for key, value in payload.items() if key != "serialization_version"}
    _strict(body, MarketEpisodeLifecycleState)
    try:
        config = MarketEpisodeLifecycleConfig(**_strict(body["lifecycle_config"],
                                                MarketEpisodeLifecycleConfig))
        scope = MarketEpisodeScope(**_strict(body["scope"], MarketEpisodeScope))
        active_payload = body["active_episode"]
        active = None if active_payload is None else ActiveMarketEpisode(
            **{**_strict(active_payload, ActiveMarketEpisode),
               "scope": MarketEpisodeScope(**_strict(active_payload["scope"], MarketEpisodeScope))})
        def candidate(name):
            value = body[name]
            return None if value is None else PendingDirection(**_strict(value, PendingDirection))
        def crossings(name):
            values = body[name]
            if not isinstance(values, list):
                raise ValueError(f"{name} must be a list")
            return tuple(PendingCrossing(**_strict(value, PendingCrossing)) for value in values)
        pace_payload = _strict(body["current_pace"], Metric)
        pace = Metric(**pace_payload)
        state = MarketEpisodeLifecycleState(
            **{**body, "lifecycle_config": config, "scope": scope,
               "current_pace": pace, "active_episode": active,
               "pending_start": candidate("pending_start"),
               "pending_reversal": candidate("pending_reversal"),
               "pending_resume": candidate("pending_resume"),
               "pending_strengthen": crossings("pending_strengthen"),
               "pending_weaken": crossings("pending_weaken")})
    except (TypeError, AttributeError, KeyError) as exc:
        raise ValueError("malformed market episode state") from exc
    if serialize_market_episode_lifecycle_state(state) != payload:
        raise ValueError("market episode state is not canonical")
    return state
