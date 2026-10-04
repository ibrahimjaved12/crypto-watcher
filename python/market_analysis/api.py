"""FastAPI service. No database client, user authentication replacement or writes."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import os
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import httpx

from .api_models import (
    AnalysisRequest,
    CompletedCandleSeriesRequest,
    MovementBoundaryRequest,
    MovementClassificationRequest,
    MovementHistoryRegistrationRequest,
    MovementLifecycleRequest,
    MovementMetricsRequest,
    TechnicalAnalysisBatchRequest,
    TechnicalAnalysisRequest,
)
from .movement_service import MovementBoundaryService
from .service import analyze_request
from .technical import calculate_technical_analysis


def create_app(token=None, analyzer=analyze_request, analysis_timeout=18):
    token = os.environ.get("PYTHON_ANALYSIS_TOKEN", "") if token is None else token
    configured = bool(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token))

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=5, follow_redirects=False,
                                    limits=httpx.Limits(max_connections=20),
                                    headers={"Accept": "application/json"}) as client:
            app.state.market_client = client
            yield

    app = FastAPI(title="Crypto Watch analysis", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.movement_boundary_service = MovementBoundaryService()

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Default validation responses echo input values. Keep personal state out.
        return JSONResponse(status_code=422, content={"detail": "Invalid analysis request"})

    async def authorize(request: Request):
        if not configured:
            raise HTTPException(503, "Analysis service is not configured")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{32,256})", request.headers.get("authorization", ""))
        if not match or not hmac.compare_digest(match[1], token):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health():
        return JSONResponse(status_code=200 if configured else 503,
                            content={"status": "ok" if configured else "not_configured"})

    @app.post("/v1/analysis", dependencies=[Depends(authorize)])
    async def analysis(body: AnalysisRequest, request: Request):
        try:
            return await asyncio.wait_for(analyzer(body, request.app.state.market_client),
                                          timeout=analysis_timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "Analysis timed out") from None
        except Exception:
            # No raw provider error, URL, token or baseline payload in responses/logs.
            raise HTTPException(502, "Analysis could not be completed") from None

    @app.post("/v1/completed-candles/validate", dependencies=[Depends(authorize)])
    async def completed_candles(body: CompletedCandleSeriesRequest):
        series = body.domain()
        candles = series.market_candles
        return {
            "schema_version": 1, "contract_version": series.contract_version,
            "provider": series.identity.provider, "exchange": series.identity.exchange,
            "instrument_id": series.identity.instrument_id, "price_type": series.identity.price_type,
            "timeframe_minutes": series.identity.timeframe_minutes,
            "observation_count": len(series.observations), "candle_count": len(candles),
            "first_open_time_ms": candles[0].open_time_ms if candles else None,
            "last_open_time_ms": candles[-1].open_time_ms if candles else None,
            "missing_open_times_ms": series.missing_open_times_ms,
        }

    @app.post("/v1/technical-analysis", dependencies=[Depends(authorize)])
    async def technical_analysis(body: TechnicalAnalysisRequest):
        return calculate_technical_analysis(body.calculation_input())

    @app.post("/v1/technical-analysis/batch", dependencies=[Depends(authorize)])
    async def technical_analysis_batch(body: TechnicalAnalysisBatchRequest):
        return {
            "schema_version": 2,
            "results": [
                calculate_technical_analysis(item.calculation_input())
                for item in body.requests
            ],
        }

    @app.post("/v1/movement/boundary", dependencies=[Depends(authorize)])
    async def movement_boundary(body: MovementBoundaryRequest, request: Request):
        try:
            return request.app.state.movement_boundary_service.advance(body)
        except ValueError:
            raise HTTPException(409, "Movement boundary could not be applied") from None

    @app.post("/v1/movement/history", dependencies=[Depends(authorize)])
    async def movement_history(body: MovementHistoryRegistrationRequest, request: Request):
        try:
            return request.app.state.movement_boundary_service.register_history(body)
        except ValueError:
            raise HTTPException(409, "Movement history could not be registered") from None

    @app.post("/v1/movement/metrics", dependencies=[Depends(authorize)])
    async def movement_metrics(body: MovementMetricsRequest, request: Request):
        try:
            return request.app.state.movement_boundary_service.calculate_metrics(body)
        except ValueError:
            raise HTTPException(409, "Movement metrics could not be calculated") from None

    @app.post("/v1/movement/classification", dependencies=[Depends(authorize)])
    async def movement_classification(body: MovementClassificationRequest, request: Request):
        try:
            return request.app.state.movement_boundary_service.calculate_assessment(body)
        except ValueError:
            raise HTTPException(409, "Movement assessment could not be calculated") from None

    @app.post("/v1/movement/lifecycle", dependencies=[Depends(authorize)])
    async def movement_lifecycle(body: MovementLifecycleRequest, request: Request):
        try:
            return request.app.state.movement_boundary_service.calculate_lifecycle(body)
        except ValueError:
            raise HTTPException(409, "Movement lifecycle could not be calculated") from None

    return app


app = create_app()
