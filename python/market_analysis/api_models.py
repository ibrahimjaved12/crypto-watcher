"""Versioned server-to-server input. Never accepts DB credentials or a user JWT."""
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .providers import SUPPORTED_SYMBOLS, instrument_id
from .technical import (
    FuturesInstrument,
    TechnicalCandle,
    TechnicalConfig,
    TechnicalInput,
)

Timestamp = Annotated[int, Field(strict=True, ge=0, le=4102444800000)]
Price = Annotated[Decimal, Field(gt=0, le=Decimal("1e20"), max_digits=40, decimal_places=20)]
Threshold = Annotated[Decimal, Field(ge=Decimal("0.1"), le=100)]
Source = Literal["binance-usdm", "okx-usdt-swap", "kraken-futures"]
Nonnegative = Annotated[Decimal, Field(ge=0, le=Decimal("1e30"), max_digits=40, decimal_places=20)]
Positive = Annotated[Decimal, Field(gt=0, le=Decimal("1e30"), max_digits=40, decimal_places=20)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnalysisSettings(InputModel):
    threshold_pct: Threshold
    cooldown_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]
    monitoring_enabled: bool = Field(strict=True)


class SavedBaseline(InputModel):
    price: Price
    at_ms: Timestamp
    source: Source
    threshold: Threshold
    last_observed_ms: Timestamp
    last_up_alert_ms: Timestamp | None
    last_down_alert_ms: Timestamp | None

    @model_validator(mode="after")
    def ordered(self):
        if self.at_ms > self.last_observed_ms:
            raise ValueError("baseline time must not follow last observation")
        if self.at_ms % 60000 or self.last_observed_ms % 60000:
            raise ValueError("candle timestamps must align to one minute")
        return self


class AnalysisRequest(InputModel):
    schema_version: Literal[1]
    symbol: str = Field(min_length=5, max_length=16)
    instrument_id: str = Field(min_length=10, max_length=64)
    settings: AnalysisSettings
    baseline: SavedBaseline | None

    @field_validator("symbol")
    @classmethod
    def supported(cls, value):
        if value not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported USDT symbol")
        return value

    @model_validator(mode="after")
    def matching_instrument(self):
        if self.instrument_id != instrument_id(self.symbol):
            raise ValueError("instrument identity does not match symbol")
        return self


class TechnicalInstrumentRequest(InputModel):
    instrument_id: str = Field(min_length=3, max_length=128)
    exchange: Source
    native_symbol: str = Field(min_length=2, max_length=64)
    market_type: Literal["futures"]
    contract_type: Literal["perpetual"]

    @model_validator(mode="after")
    def exact_identity(self):
        if self.instrument_id != f"{self.exchange}:{self.native_symbol}":
            raise ValueError("instrument identity does not match exchange contract")
        return self


class TechnicalCandleRequest(InputModel):
    open_ms: Timestamp
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Nonnegative
    complete: bool = Field(strict=True)

    def domain(self):
        return TechnicalCandle(
            open_ms=self.open_ms,
            open=float(self.open),
            high=float(self.high),
            low=float(self.low),
            close=float(self.close),
            volume=float(self.volume),
            complete=self.complete,
        )


class TechnicalConfigurationRequest(InputModel):
    ta_version: Literal["ta-v2"]
    interpretation_version: Literal["interpretation-v1"]
    minimum_history: Literal[200]


class TechnicalAnalysisRequest(InputModel):
    schema_version: Literal[2]
    instrument: TechnicalInstrumentRequest
    timeframe_minutes: Literal[15, 60, 240]
    candles: Annotated[
        tuple[TechnicalCandleRequest, ...], Field(min_length=1, max_length=1000)
    ]
    warmup_candles: Annotated[
        tuple[TechnicalCandleRequest, ...], Field(max_length=1000)
    ] = ()
    missing_open_times_ms: Annotated[
        tuple[Timestamp, ...], Field(max_length=1000)
    ] = ()
    source: Source
    source_event_time_ms: Timestamp | None = None
    evaluation_time_ms: Timestamp
    detection_time_ms: Timestamp
    price_type: Literal["trade", "mark", "index"]
    target_candle_open_time_ms: Timestamp | None = None
    config: TechnicalConfigurationRequest

    @model_validator(mode="after")
    def ordered_and_matching(self):
        if self.source != self.instrument.exchange:
            raise ValueError("candle source does not match the futures contract")
        if self.source_event_time_ms is not None and self.source_event_time_ms > self.evaluation_time_ms:
            raise ValueError("source event time must not follow evaluation time")
        if self.detection_time_ms > self.evaluation_time_ms:
            raise ValueError("detection time must not follow evaluation time")
        return self

    def calculation_input(self):
        return TechnicalInput(
            instrument=FuturesInstrument(**self.instrument.model_dump()),
            timeframe_minutes=self.timeframe_minutes,
            candles=tuple(candle.domain() for candle in self.candles),
            warmup_candles=tuple(candle.domain() for candle in self.warmup_candles),
            missing_open_times_ms=self.missing_open_times_ms,
            source=self.source,
            source_event_time_ms=self.source_event_time_ms,
            evaluation_time_ms=self.evaluation_time_ms,
            detection_time_ms=self.detection_time_ms,
            price_type=self.price_type,
            target_candle_open_time_ms=self.target_candle_open_time_ms,
            config=TechnicalConfig(**self.config.model_dump()),
        )


class TechnicalAnalysisBatchRequest(InputModel):
    schema_version: Literal[2]
    requests: Annotated[
        tuple[TechnicalAnalysisRequest, ...], Field(min_length=1, max_length=8)
    ]


class MovementObservationRequest(InputModel):
    price: Positive
    quantity: Positive
    event_time_ms: Timestamp
    trade_time_ms: Timestamp
    aggregate_trade_id: Annotated[int, Field(strict=True, ge=0)]
    received_at_ms: Timestamp


class MovementSymbolBoundaryRequest(InputModel):
    symbol: str = Field(min_length=5, max_length=16)
    instrument_id: str = Field(min_length=3, max_length=128)
    membership_epoch: Annotated[int, Field(strict=True, ge=1)]
    source_state: Literal["LIVE", "RECOVERING", "STALE", "UNAVAILABLE"]
    observations: Annotated[tuple[MovementObservationRequest, ...], Field(max_length=20_000)]

    @model_validator(mode="after")
    def exact_instrument(self):
        if self.symbol not in SUPPORTED_SYMBOLS:
            raise ValueError("unsupported USDT symbol")
        if self.instrument_id != instrument_id(self.symbol):
            raise ValueError("instrument identity does not match symbol")
        previous_key = None
        for observation in self.observations:
            key = (observation.trade_time_ms, observation.aggregate_trade_id)
            if previous_key is not None and key <= previous_key:
                raise ValueError("movement observations must be strictly ordered")
            previous_key = key
        return self


class MovementBoundaryRequest(InputModel):
    schema_version: Literal[1]
    session_id: UUID
    boundary_time_ms: Timestamp
    symbols: Annotated[tuple[MovementSymbolBoundaryRequest, ...], Field(min_length=1, max_length=100)]

    @model_validator(mode="after")
    def aligned_unique_symbols(self):
        if self.boundary_time_ms % 5_000:
            raise ValueError("movement boundary must align to five seconds")
        symbols = [item.symbol for item in self.symbols]
        if len(symbols) != len(set(symbols)):
            raise ValueError("movement boundary symbols must be unique")
        return self


class MovementMetricsConfigRequest(InputModel):
    version: str = Field(min_length=1, max_length=128)
    historical_lookback_ms: Annotated[int, Field(strict=True, gt=0)]
    minimum_historical_coverage_ms: Annotated[int, Field(strict=True, gt=0)]
    flat_z: float
    material_z: float
    trim_fraction: float
    liquidity_weight_cap: float
    rvol_comparison_windows: Annotated[int, Field(strict=True, gt=0)]
    outlier_cross_z: float
    outlier_historical_z: float
    minimum_eligible_fraction: float
    minimum_eligible_count: Annotated[int, Field(strict=True, gt=0)]


class MovementCompletedCandleRequest(InputModel):
    open_time_ms: Timestamp
    close: Price
    volume: Nonnegative
    quote_volume: Nonnegative

    @model_validator(mode="after")
    def aligned(self):
        if self.open_time_ms % 60_000:
            raise ValueError("completed movement candle must align to one minute")
        return self


class MovementHistoricalSymbolRequest(InputModel):
    symbol: str = Field(min_length=5, max_length=16)
    instrument_compatible: bool | None = Field(strict=True)
    candles: Annotated[tuple[MovementCompletedCandleRequest, ...], Field(max_length=10_110)]


class MovementHistoryRegistrationRequest(InputModel):
    schema_version: Literal[1]
    session_id: UUID
    history_version: str = Field(min_length=1, max_length=128)
    as_of_boundary_time_ms: Timestamp
    universe_id: str = Field(min_length=1, max_length=128)
    universe_version: str = Field(min_length=1, max_length=128)
    symbols: Annotated[tuple[str, ...], Field(min_length=1, max_length=100)]
    config: MovementMetricsConfigRequest
    historical: Annotated[tuple[MovementHistoricalSymbolRequest, ...], Field(max_length=100)]

    @model_validator(mode="after")
    def exact_symbols(self):
        if self.as_of_boundary_time_ms % 5_000:
            raise ValueError("history as-of boundary must align to five seconds")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("movement universe symbols must be unique")
        if tuple(item.symbol for item in self.historical) != self.symbols:
            raise ValueError("historical symbol set must match configured universe")
        if any(symbol not in SUPPORTED_SYMBOLS for symbol in self.symbols):
            raise ValueError("unsupported movement symbol")
        return self


class MovementMetricsRequest(InputModel):
    schema_version: Literal[1]
    session_id: UUID
    evaluation_boundary_time_ms: Timestamp
    history_version: str = Field(min_length=1, max_length=128)
    universe_id: str = Field(min_length=1, max_length=128)
    universe_version: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def aligned(self):
        if self.evaluation_boundary_time_ms % 5_000:
            raise ValueError("movement metrics boundary must align to five seconds")
        return self


class PreviousConfirmedPrimaryEpisode(InputModel):
    direction: Literal["BROAD_RISE", "BROAD_DROP"]
    universe_id: str = Field(min_length=1, max_length=128)
    universe_version: str = Field(min_length=1, max_length=128)
    movement_algorithm_version: str = Field(min_length=1, max_length=128)
    movement_config_version: str = Field(min_length=1, max_length=128)
    classifier_algorithm_version: str = Field(min_length=1, max_length=128)
    classifier_config_version: str = Field(min_length=1, max_length=128)


class MovementClassificationRequest(MovementMetricsRequest):
    previous_confirmed_primary_episode: PreviousConfirmedPrimaryEpisode | None = None
