"""In-memory transport adapter around the pure movement bucket engine."""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256

from .api_models import MovementBoundaryRequest
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
        if type(session_capacity) is not int or session_capacity < 1:
            raise ValueError("session_capacity must be a positive integer")
        self.cache_capacity = cache_capacity
        self.session_capacity = session_capacity
        self.sessions = OrderedDict()

    @staticmethod
    def _new_session():
        return {
            "engines": {},
            "late_after_finalization_count": 0,
            "responses": OrderedDict(),
            "response_hashes": {},
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

        active_symbols = {item.symbol for item in request.symbols}
        previous_late_total = sum(
            session["engines"][item.symbol].rejected_late_observations
            for item in request.symbols
            if item.symbol in session["engines"]
        )
        staged_engines = {
            symbol: deepcopy(engine) for symbol, engine in session["engines"].items()
            if symbol in active_symbols
        }
        snapshots = []
        for item in request.symbols:
            engine = staged_engines.get(item.symbol)
            if engine is None:
                engine = MovementBucketEngine(
                    item.instrument_id,
                    provider=BINANCE_USDM,
                    price_type=TRADE_PRICE,
                    capacity=DEFAULT_HISTORY_BUCKETS,
                )
                staged_engines[item.symbol] = engine
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
        session["late_after_finalization_count"] = response["lateAfterFinalizationCount"]
        session["responses"][cache_key] = response
        session["response_hashes"][cache_key] = fingerprint
        while len(session["responses"]) > self.cache_capacity:
            expired, _ = session["responses"].popitem(last=False)
            session["response_hashes"].pop(expired, None)
        self._commit_session(session_id, session, is_new_session)
        return response

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