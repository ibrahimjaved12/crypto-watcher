"""In-memory transport adapter around the pure movement bucket engine."""
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
from decimal import Decimal
from hashlib import sha256
import json

from .api_models import (MovementBoundaryRequest, MovementHistoryRegistrationRequest,
                         MovementMetricsRequest, MovementClassificationRequest,
                         MovementLifecycleRequest)
from .market_episode_lifecycle import (
    deserialize_market_episode_lifecycle_state,
    interrupt_market_episode_state_on_restart, process_market_episode_lifecycle,
    serialize_market_episode_lifecycle_state, serialize_market_episode_transition,
)
from .movement_classifier import (
    ALGORITHM_VERSION as CLASSIFIER_ALGORITHM_VERSION,
    MarketClassifierConfig, MarketClassificationContext, MarketWindowClassificationContext,
    SymbolSourceTimeEvidence, classify_market_movement,
)
from .movement import (
    BINANCE_USDM,
    BUCKET_INTERVAL_MS,
    DEFAULT_HISTORY_BUCKETS,
    MAX_LAST_TRADE_AGE_MS,
    TRADE_PRICE,
    WINDOW_BUCKETS,
    MarketObservation,
    MovementBucketEngine,
)
from .movement_metrics import (MarketMovementConfig,
                               MarketMovementInput, MarketMovementSymbolInput,
                               MarketUniverseInput, calculate_market_movement)
from .movement_history import CompletedMovementCandle, build_historical_window_inputs

DEFAULT_RESPONSE_CACHE_CAPACITY = 4
DEFAULT_SESSION_CAPACITY = 4


class MovementBoundaryService:
    """Owns ephemeral per-session engines and idempotent boundary responses."""

    def __init__(
        self,
        cache_capacity=DEFAULT_RESPONSE_CACHE_CAPACITY,
        session_capacity=DEFAULT_SESSION_CAPACITY,
    ):
        if type(cache_capacity) is not int or cache_capacity < 1:
            raise ValueError("cache_capacity must be a positive integer")
        if type(session_capacity) is not int or session_capacity < 2:
            raise ValueError("session_capacity must be an integer of at least two")
        self.cache_capacity = cache_capacity
        self.session_capacity = session_capacity
        self.sessions = OrderedDict()

    @staticmethod
    def _new_session():
        return {
            "engines": {},
            "membership_epochs": {},
            "late_after_finalization_count": 0,
            "responses": OrderedDict(),
            "response_hashes": {},
            "history": None,
        }

    def _commit_session(self, session_id, session, is_new):
        if is_new:
            self.sessions[session_id] = session
            while len(self.sessions) > self.session_capacity:
                self.sessions.popitem(last=False)
        else:
            self.sessions.move_to_end(session_id)

    def advance(self, request: MovementBoundaryRequest):
        fingerprint = sha256(request.model_dump_json().encode()).hexdigest()
        cache_key = (str(request.session_id), request.boundary_time_ms)
        session_id = str(request.session_id)
        session = self.sessions.get(session_id)
        is_new_session = session is None
        if is_new_session:
            session = self._new_session()
        cached = session["responses"].get(cache_key)
        if cached is not None:
            if session["response_hashes"][cache_key] != fingerprint:
                raise ValueError("movement boundary was already applied with different input")
            if not is_new_session:
                self.sessions.move_to_end(session_id)
            session["responses"].move_to_end(cache_key)
            return cached

        previous_late_total = sum(
            session["engines"][item.symbol].rejected_late_observations
            for item in request.symbols
            if (
                item.symbol in session["engines"]
                and session["membership_epochs"].get(item.symbol) == item.membership_epoch
            )
        )
        staged_engines = {}
        staged_membership_epochs = {}
        snapshots = []
        for item in request.symbols:
            engine = None
            # Membership tenure is transport ownership metadata. It selects the
            # canonical engine instance but never enters bucket mathematics.
            if session["membership_epochs"].get(item.symbol) == item.membership_epoch:
                existing = session["engines"].get(item.symbol)
                if existing is not None:
                    engine = deepcopy(existing)
            if engine is None:
                engine = MovementBucketEngine(
                    item.instrument_id,
                    provider=BINANCE_USDM,
                    price_type=TRADE_PRICE,
                    capacity=DEFAULT_HISTORY_BUCKETS,
                )
            staged_engines[item.symbol] = engine
            staged_membership_epochs[item.symbol] = item.membership_epoch
            observations = tuple(
                MarketObservation(
                    provider=BINANCE_USDM,
                    instrument_id=item.instrument_id,
                    price_type=TRADE_PRICE,
                    price=value.price,
                    quantity=value.quantity,
                    event_time_ms=value.event_time_ms,
                    trade_time_ms=value.trade_time_ms,
                    aggregate_trade_id=value.aggregate_trade_id,
                    received_at_ms=value.received_at_ms,
                )
                for value in item.observations
            )
            engine.observe(observations)
            engine.advance(request.boundary_time_ms, item.source_state)
            snapshots.append(self._snapshot(item.symbol, engine, request.boundary_time_ms,
                                            item.source_state))

        response = {
            "sessionId": session_id,
            "boundaryTime": request.boundary_time_ms,
            "lateAfterFinalizationCount": session["late_after_finalization_count"] + max(
                0,
                sum(engine.rejected_late_observations for engine in staged_engines.values())
                - previous_late_total,
            ),
            "snapshots": snapshots,
        }
        session["engines"] = staged_engines
        session["membership_epochs"] = staged_membership_epochs
        session["late_after_finalization_count"] = response["lateAfterFinalizationCount"]
        session["responses"][cache_key] = response
        session["response_hashes"][cache_key] = fingerprint
        while len(session["responses"]) > self.cache_capacity:
            expired, _ = session["responses"].popitem(last=False)
            session["response_hashes"].pop(expired, None)
        self._commit_session(session_id, session, is_new_session)
        return response

    def register_history(self, request: MovementHistoryRegistrationRequest):
        session_id = str(request.session_id)
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError("movement session is unavailable")
        config = MarketMovementConfig(**request.config.model_dump())
        universe = MarketUniverseInput(request.universe_id, request.universe_version,
                                       request.symbols)
        fingerprint = sha256(json.dumps(request.model_dump(mode="json"),
                                        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        previous = session["history"]
        if previous is not None and previous["version"] == request.history_version:
            if previous["fingerprint"] != fingerprint:
                raise ValueError("history version was already registered with different input")
        else:
            historical = {
                item.symbol: {
                    "instrument_compatible": item.instrument_compatible,
                    "candles": tuple(CompletedMovementCandle(
                        candle.open_time_ms, candle.close, candle.volume,
                        candle.quote_volume,
                    ) for candle in item.candles),
                }
                for item in request.historical
            }
            # One slot per bounded movement session; replacement releases old history.
            session["history"] = {
                "version": request.history_version,
                "as_of_boundary": request.as_of_boundary_time_ms,
                "fingerprint": fingerprint,
                "universe": universe,
                "config": config,
                "historical": historical,
            }
        self.sessions.move_to_end(session_id)
        return {"schema_version": 1, "session_id": session_id,
                "history_version": request.history_version,
                "universe_id": universe.id, "universe_version": universe.version,
                "as_of_boundary_time_ms": request.as_of_boundary_time_ms}

    def calculate_metrics(self, request: MovementMetricsRequest):
        session_id, history_version, result, _ = self._evaluate_movement(request)
        return {"schema_version": 1, "session_id": session_id,
                "history_version": history_version,
                "evaluation": self._transport(result)}

    def calculate_assessment(self, request: MovementClassificationRequest):
        session_id, history_version, result, endpoints = self._evaluate_movement(request)
        previous = request.previous_confirmed_primary_episode
        classifier_config = MarketClassifierConfig()
        effective_prior_direction = self._prior_direction(
            result, classifier_config, previous.direction if previous else None, previous,
        )
        classification = self._classify(result, endpoints, classifier_config,
                                         effective_prior_direction)
        return {"schema_version": 1, "session_id": session_id,
                "history_version": history_version,
                "effective_previous_confirmed_primary_direction": effective_prior_direction,
                "evaluation": self._transport(result),
                "classification": self._transport(classification)}

    def calculate_lifecycle(self, request: MovementLifecycleRequest):
        previous = (deserialize_market_episode_lifecycle_state(request.previous_lifecycle_state)
                    if request.previous_lifecycle_state is not None else None)
        if request.interrupt_previous_state and previous is not None:
            previous = interrupt_market_episode_state_on_restart(previous)
        session_id, history_version, result, endpoints = self._evaluate_movement(request)
        classifier_config = MarketClassifierConfig()
        active = previous.active_episode if previous is not None else None
        effective_prior_direction = self._prior_direction(
            result, classifier_config, active.direction if active else None,
            active.scope if active else None,
        )
        classification = self._classify(result, endpoints, classifier_config,
                                         effective_prior_direction)
        lifecycle = process_market_episode_lifecycle(classification, previous)
        state = lifecycle.next_state
        scope = state.scope
        return {"schema_version": 1, "session_id": session_id,
                "history_version": history_version,
                "effective_previous_confirmed_primary_direction": effective_prior_direction,
                "evaluation": self._transport(result),
                "classification": self._transport(classification),
                "lifecycle": {
                    "schema_version": 1,
                    "serialized_state": serialize_market_episode_lifecycle_state(state),
                    "state_summary": {
                        "evaluation_boundary_time_ms": state.last_evaluation_boundary_time_ms,
                        "current_direction_state": state.current_direction_state,
                        "current_pace": self._transport(state.current_pace),
                        "active_episode_id": (state.active_episode.episode_id
                                              if state.active_episode else None),
                        "active_episode_direction": (state.active_episode.direction
                                                     if state.active_episode else None),
                        "interrupted": state.interrupted,
                        "lifecycle_algorithm_version": state.lifecycle_algorithm_version,
                        "lifecycle_config_version": state.lifecycle_config_version,
                        "universe_id": scope.universe_id,
                        "universe_version": scope.universe_version,
                        "primary_window_minutes": scope.primary_window_minutes,
                        "classifier_algorithm_version": scope.classifier_algorithm_version,
                        "classifier_config_version": scope.classifier_config_version,
                        "movement_algorithm_version": scope.movement_algorithm_version,
                        "movement_config_version": scope.movement_config_version,
                        "provider": scope.provider, "exchange": scope.exchange,
                        "price_type": scope.price_type,
                    },
                    "transitions": [serialize_market_episode_transition(event)
                                    for event in lifecycle.transitions],
                }}

    @staticmethod
    def _prior_direction(result, classifier_config, direction, scope):
        return (direction if scope is not None
                and scope.universe_id == result.universe_id
                and scope.universe_version == result.universe_version
                and scope.movement_algorithm_version == result.algorithm_version
                and scope.movement_config_version == result.config_version
                and scope.classifier_algorithm_version == CLASSIFIER_ALGORITHM_VERSION
                and scope.classifier_config_version == classifier_config.version
                else None)

    @staticmethod
    def _classify(result, endpoints, classifier_config, effective_prior_direction):
        provenance = tuple(SymbolSourceTimeEvidence(
            symbol=symbol,
            last_real_trade_time_ms=(endpoints[symbol].last_real_trade_time_ms
                                     if endpoints[symbol] else None),
            last_real_event_time_ms=(endpoints[symbol].last_real_event_time_ms
                                     if endpoints[symbol] else None),
            last_received_at_ms=(endpoints[symbol].last_received_at_ms
                                 if endpoints[symbol] else None),
        ) for symbol in result.configured_universe)
        context = MarketClassificationContext({
            window: MarketWindowClassificationContext(
                source_time_evidence=provenance,
                prior_confirmed_episode_direction=(
                    effective_prior_direction if window == 5 else None
                ),
            ) for window in (1, 5, 15)
        })
        return classify_market_movement(result, context, classifier_config)

    def _evaluate_movement(self, request: MovementMetricsRequest):
        session_id = str(request.session_id)
        session = self.sessions.get(session_id)
        if session is None or session["history"] is None:
            raise ValueError("movement session or history is unavailable")
        registration = session["history"]
        universe = registration["universe"]
        if (request.history_version != registration["version"]
                or request.universe_id != universe.id
                or request.universe_version != universe.version):
            raise ValueError("movement history identity does not match")
        boundary = request.evaluation_boundary_time_ms
        if boundary < registration["as_of_boundary"]:
            raise ValueError("movement boundary predates registered history cutoff")
        engines = session["engines"]
        symbol_inputs = {}
        endpoints = {}
        found_boundary = False
        for symbol in universe.symbols:
            engine = engines.get(symbol)
            if engine is None:
                endpoints[symbol] = None
                continue
            endpoint = next((bucket for bucket in engine.history
                             if bucket.boundary_time_ms == boundary), None)
            endpoints[symbol] = endpoint
            if endpoint is None:
                # A newly joined membership may have no engine history for an
                # older catch-up boundary. #71 represents that symbol as missing.
                continue
            found_boundary = True
            readiness = {window: engine.readiness(boundary, window, endpoint.source_state)
                         for window in (1, 5, 15)}
            history = registration["historical"][symbol]
            symbol_inputs[symbol] = MarketMovementSymbolInput(
                symbol=symbol, instrument_id=engine.instrument_id,
                instrument_compatible=history["instrument_compatible"],
                readiness=readiness,
                historical=build_historical_window_inputs(
                    history["candles"], boundary, registration["config"]),
            )
        if not found_boundary:
            raise ValueError("requested boundary is not finalized in this session")
        result = calculate_market_movement(MarketMovementInput(
            boundary, universe, symbol_inputs, registration["config"]
        ))
        self.sessions.move_to_end(session_id)
        return session_id, registration["version"], result, endpoints

    @staticmethod
    def _transport(value):
        if is_dataclass(value):
            return {field.name: MovementBoundaryService._transport(getattr(value, field.name))
                    for field in fields(value)}
        if isinstance(value, Mapping):
            return {str(key): MovementBoundaryService._transport(item)
                    for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [MovementBoundaryService._transport(item) for item in value]
        if isinstance(value, Decimal):
            return float(value)
        return value

    @staticmethod
    def _snapshot(symbol, engine, boundary_time_ms, source_state):
        buckets = []
        for bucket in engine.history:
            buckets.append({
                "boundaryTime": bucket.boundary_time_ms,
                "sourceState": bucket.source_state,
                "endpointPrice": None if bucket.price is None else float(bucket.price),
                "baseQuantity": float(bucket.base_volume),
                "quoteVolume": float(bucket.quote_volume),
                "tradeCount": bucket.trade_count,
                "lastRealTradeTime": bucket.last_real_trade_time_ms,
                "lastRealEventTime": bucket.last_real_event_time_ms,
                "lastRealReceivedAt": bucket.last_received_at_ms,
                "carriedForward": bucket.carried_forward,
                "provider": bucket.provider,
                "instrumentId": bucket.instrument_id,
                "nativeSymbol": symbol,
                "symbol": symbol,
                "marketType": "futures",
                "contractType": "perpetual",
                "priceType": bucket.price_type,
            })
        readiness = {}
        for window_minutes in WINDOW_BUCKETS:
            result = engine.readiness(boundary_time_ms, window_minutes, source_state)
            readiness[window_minutes] = {
                "windowMinutes": window_minutes,
                "state": result.state,
                "reason": result.reason,
                "status": {
                    "ready": "READY",
                    "warming": "WARMING",
                }.get(result.state, "STALE"),
            }
        latest = buckets[-1] if buckets else None
        return {
            "symbol": symbol,
            "provider": BINANCE_USDM,
            "instrumentId": f"{BINANCE_USDM}:{symbol}",
            "priceType": TRADE_PRICE,
            "bucketMs": BUCKET_INTERVAL_MS,
            "maxLastTradeAgeMs": MAX_LAST_TRADE_AGE_MS,
            "buckets": buckets,
            "latestRealTradeTime": None if latest is None else latest["lastRealTradeTime"],
            "latestRealReceivedAt": None if latest is None else latest["lastRealReceivedAt"],
            "readiness": readiness,
        }
