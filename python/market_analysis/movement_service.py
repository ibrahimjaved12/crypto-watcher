"""In-memory transport adapter around the pure movement bucket engine."""
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass
from decimal import Decimal
from hashlib import sha256
import json

from .api_models import (MovementBoundaryRequest, MovementHistoryRegistrationRequest,
                         MovementMetricsRequest)
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
from .movement_metrics import (HistoricalWindowInput, MarketMovementConfig,
                               MarketMovementInput, MarketMovementSymbolInput,
                               MarketUniverseInput, calculate_market_movement)

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
                    int(window): HistoricalWindowInput(
                        values.returns, values.usable_coverage_ms,
                        values.previous_notional_volumes,
                    )
                    for window, values in item.windows.items()
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
        found_boundary = False
        for symbol in universe.symbols:
            engine = engines.get(symbol)
            if engine is None:
                continue
            endpoint = next((bucket for bucket in engine.history
                             if bucket.boundary_time_ms == boundary), None)
            if endpoint is None:
                # A newly joined membership may have no engine history for an
                # older catch-up boundary. #71 represents that symbol as missing.
                continue
            found_boundary = True
            readiness = {window: engine.readiness(boundary, window, endpoint.source_state)
                         for window in (1, 5, 15)}
            symbol_inputs[symbol] = MarketMovementSymbolInput(
                symbol=symbol, instrument_id=engine.instrument_id,
                instrument_compatible=engine.instrument_id == f"{BINANCE_USDM}:{symbol}",
                readiness=readiness,
                historical=registration["historical"].get(symbol, {}),
            )
        if not found_boundary:
            raise ValueError("requested boundary is not finalized in this session")
        result = calculate_market_movement(MarketMovementInput(
            boundary, universe, symbol_inputs, registration["config"]
        ))
        self.sessions.move_to_end(session_id)
        return {"schema_version": 1, "session_id": session_id,
                "history_version": registration["version"],
                "evaluation": self._transport(result)}

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
