"""Versioned server-to-server input. Never accepts DB credentials or a user JWT."""
from decimal import Decimal
from typing import Annotated, Any, ClassVar, Literal, Optional
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


class MovementLifecycleRequest(MovementMetricsRequest):
    previous_lifecycle_state: dict[str, Any] | None
    interrupt_previous_state: bool = Field(strict=True)


# #91 uses safe millisecond integers and exact Decimal facts without imposing
# unrelated TA/analysis numerical precision or date limits.
CandleTimestamp = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]


class CompletedCandleIdentityRequest(InputModel):
    provider: Literal["binance-usdm"]
    exchange: Literal["binance"]
    market_type: Literal["futures"]
    contract_type: Literal["perpetual"]
    instrument_id: str
    symbol: str
    native_symbol: str
    price_type: Literal["trade"]
    series_basis: Literal["native-kline"]
    timeframe_minutes: Annotated[int, Field(strict=True)]

    def domain(self):
        from .completed_candles import CompletedCandleSeriesIdentity
        return CompletedCandleSeriesIdentity(**self.model_dump())


class WebSocketCandleProvenanceRequest(InputModel):
    source_kind: Literal["websocket"]
    endpoint: str
    source_event_time_ms: CandleTimestamp
    received_at_ms: CandleTimestamp

    def domain(self):
        from .completed_candles import WebSocketCandleProvenance
        return WebSocketCandleProvenance(**self.model_dump(exclude={"source_kind"}))


class RestCandleProvenanceRequest(InputModel):
    source_kind: Literal["rest"]
    endpoint: str
    retrieved_at_ms: CandleTimestamp

    def domain(self):
        from .completed_candles import RestCandleProvenance
        return RestCandleProvenance(**self.model_dump(exclude={"source_kind"}))


class ArchiveCandleProvenanceRequest(InputModel):
    source_kind: Literal["archive"]
    dataset_id: str
    dataset_version: str
    dataset_content_sha256: str

    def domain(self):
        from .completed_candles import ArchiveCandleProvenance
        return ArchiveCandleProvenance(**self.model_dump(exclude={"source_kind"}))


class CompletedCandleMarketRequest(InputModel):
    open_time_ms: CandleTimestamp
    close_time_ms: CandleTimestamp
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal


class CompletedCandleObservationRequest(InputModel):
    candle: CompletedCandleMarketRequest
    provenance: Annotated[
        WebSocketCandleProvenanceRequest | RestCandleProvenanceRequest | ArchiveCandleProvenanceRequest,
        Field(discriminator="source_kind")]


class CompletedCandleSeriesRequest(InputModel):
    contract_version: Literal["completed-candle-v1"]
    identity: CompletedCandleIdentityRequest
    observations: tuple[CompletedCandleObservationRequest, ...]

    def domain(self):
        from .completed_candles import CompletedCandle, CompletedCandleObservation, CompletedCandleSeries
        identity = self.identity.domain()
        return CompletedCandleSeries(self.contract_version, identity, tuple(
            CompletedCandleObservation(CompletedCandle(identity=identity, **item.candle.model_dump()),
                                       item.provenance.domain()) for item in self.observations))

    @model_validator(mode="after")
    def valid_domain(self):
        self.domain()
        return self


# ---------------------------------------------------------------- forward engine (#239 P10)

ForwardFloat = Annotated[float, Field(strict=False, ge=0, allow_inf_nan=False)]


class ForwardCollectorRow(InputModel):
    """One completed 1-minute ``collector_recent_candles`` row as read by the application."""
    open_time_ms: Timestamp
    close_time_ms: Timestamp | None = None
    open: ForwardFloat
    high: ForwardFloat
    low: ForwardFloat
    close: ForwardFloat
    volume: ForwardFloat = 0.0
    transport: Literal["rest", "websocket"]
    endpoint: str | None = Field(default=None, max_length=128)
    source_event_at_ms: Timestamp | None = None
    timeframe_minutes: Literal[1] = 1


class ForwardFundingEvent(InputModel):
    calc_time_ms: Timestamp
    rate: Decimal = Field(ge=Decimal("-1"), le=Decimal("1"))
    interval_hours: Annotated[int, Field(strict=True, ge=1, le=24)] = 8
    # The exchange's mark price at the settlement (Binance fundingRate `markPrice`); when present it is the
    # settlement mark of that minute instead of the trade-price proxy.
    mark: Decimal | None = Field(default=None, gt=Decimal("0"))


class ForwardSymbolInput(InputModel):
    symbol: str = Field(min_length=5, max_length=16, pattern=r"^[A-Z0-9]+$")
    # P16: about 120 days of 1m history (ewma-robust-hcal warm-up) = 172,800 rows.
    rows: Annotated[tuple[ForwardCollectorRow, ...], Field(min_length=1, max_length=200_000)]
    funding: Annotated[tuple[ForwardFundingEvent, ...], Field(max_length=1000)] = ()
    # False when the caller could not fetch funding history: windows that contain a possible funding
    # time are then left open instead of being finalised with zero funding.
    funding_available: bool = True


class ForwardOpenSetup(InputModel):
    setup: dict[str, Any]
    resolution: dict[str, Any] | None = None


class ForwardEvaluateRequest(InputModel):
    schema_version: Literal[1]
    symbols: Annotated[tuple[ForwardSymbolInput, ...], Field(min_length=1, max_length=20)]
    strategy_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=200)]
    from_ms: Timestamp
    to_ms: Timestamp
    open_setups: Annotated[tuple[ForwardOpenSetup, ...], Field(max_length=2000)] = ()
    wallet_state: dict[str, Any] | None = None

    @model_validator(mode="after")
    def window(self):
        if self.to_ms < self.from_ms:
            raise ValueError("to_ms precedes from_ms")
        if len({item.symbol for item in self.symbols}) != len(self.symbols):
            raise ValueError("duplicate symbol")
        return self

    def evaluate_input(self) -> dict:
        return {
            "symbols": [{"symbol": item.symbol, "rows": [row.model_dump() for row in item.rows],
                         "funding": [{"calc_time_ms": f.calc_time_ms, "rate": str(f.rate),
                                      "interval_hours": f.interval_hours,
                                      **({"mark": str(f.mark)} if f.mark is not None else {})} for f in item.funding],
                         "funding_available": item.funding_available}
                        for item in self.symbols],
            "strategy_ids": list(self.strategy_ids), "from_ms": self.from_ms, "to_ms": self.to_ms,
            "open_setups": [item.model_dump() for item in self.open_setups], "wallet_state": self.wallet_state,
        }


class ForwardWalletConfig(InputModel):
    """Exact wallet configuration shared by the two split endpoints.

    ``wallet_version`` selects the run configuration: wallet-v2 (default, unchanged) or wallet-v3 (#36), whose
    extra options are refused under wallet-v2 so a v2 run can never be changed by a stray field.
    """
    initial_balance_e8: Annotated[int, Field(strict=True, gt=0)] = 100 * 10**8
    risk_fraction: str = "1/100"
    max_positions: Annotated[int, Field(strict=True, ge=1, le=2000)] = 6
    max_exposure_multiple: str = "5"
    leverage_cap: Annotated[int, Field(strict=True, ge=1, le=125)] = 20
    liquidation_buffer: Annotated[int, Field(strict=True, ge=1)] = 2
    taker_rate: str = "0.0005"
    slip_floor_bps: str = "1"
    mmr: str = "0.01"
    wallet_version: Literal["wallet-v2", "wallet-v3"] = "wallet-v2"
    maker_rate: str = "0.0002"
    sizing_basis: Literal["balance", "equity"] = "balance"
    ledger_links: bool = False
    run_id: Annotated[str, Field(max_length=128)] = ""
    # Part 2: ``contract_inputs`` loads the versioned data file (its hash is recorded); the limits are optional
    # exact fractions of the balance, and ``portfolio_limits`` switches on both at their defaults (5 % total open
    # risk; the cluster cap is a labelled placeholder). Explicit values win over ``portfolio_limits``.
    contract_inputs: bool = False
    # A stored run configuration pins the data file it ran with; a different file is a new run, never a silent swap.
    contract_inputs_version: str | None = None
    contract_inputs_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    portfolio_limits: bool = False
    max_open_risk_fraction: str | None = None
    cluster_risk_fraction: str | None = None
    cost_aware_sizing: bool = False

    V3_ONLY: ClassVar[frozenset] = frozenset({
        "maker_rate", "sizing_basis", "ledger_links", "run_id", "contract_inputs", "contract_inputs_version",
        "contract_inputs_hash", "portfolio_limits", "max_open_risk_fraction", "cluster_risk_fraction",
        "cost_aware_sizing"})

    @field_validator("max_open_risk_fraction", "cluster_risk_fraction")
    @classmethod
    def exact_fraction_of_balance(cls, value):
        from fractions import Fraction
        if value is None:
            return value
        try:
            number = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError("expected an exact fraction in (0, 1]") from None
        if not 0 < number <= 1:
            raise ValueError("expected an exact fraction in (0, 1]")
        return value

    @field_validator("risk_fraction", "max_exposure_multiple", "taker_rate", "slip_floor_bps", "mmr", "maker_rate")
    @classmethod
    def exact_nonnegative(cls, value):
        from fractions import Fraction
        try:
            number = Fraction(value)
        except (ValueError, ZeroDivisionError):
            raise ValueError("expected an exact nonnegative fraction") from None
        if number < 0:
            raise ValueError("expected an exact nonnegative fraction")
        return value

    @model_validator(mode="after")
    def v3_options_need_v3(self):
        if self.wallet_version == "wallet-v2" and self.model_fields_set & self.V3_ONLY:
            raise ValueError("option requires wallet_version wallet-v3")
        return self

    def config(self):
        from fractions import Fraction
        from .forward.wallet import WalletConfig
        values = self.model_dump()
        for key in ("risk_fraction", "max_exposure_multiple", "taker_rate", "slip_floor_bps", "mmr", "maker_rate"):
            values[key] = Fraction(values[key])
        version = values.pop("wallet_version")
        if version == "wallet-v2":
            for key in self.V3_ONLY:
                values.pop(key)
            return WalletConfig(**values)
        from .forward import contract_inputs
        from .forward.wallet_v3 import DEFAULT_MAX_OPEN_RISK, PLACEHOLDER_CLUSTER_RISK, WalletV3Config
        pinned = (values.pop("contract_inputs_version"), values.pop("contract_inputs_hash"))
        values["contract_inputs"] = contract_inputs.load() if values["contract_inputs"] else None
        loaded = values["contract_inputs"]
        if loaded is not None and any(pin is not None for pin in pinned) and pinned not in (
                (loaded.version, loaded.sha256), (None, loaded.sha256), (loaded.version, None)):
            raise ValueError("contract inputs differ from the stored run configuration (a new run is needed)")
        limits = values.pop("portfolio_limits")
        total, cluster = values["max_open_risk_fraction"], values["cluster_risk_fraction"]
        values["max_open_risk_fraction"] = Fraction(total) if total is not None else (
            DEFAULT_MAX_OPEN_RISK if limits else None)
        values["cluster_risk_fraction"] = Fraction(cluster) if cluster is not None else (
            PLACEHOLDER_CLUSTER_RISK if limits else None)
        return WalletV3Config(**values)


class ForwardMarketRequest(ForwardSymbolInput):
    sigma_state: dict[str, Any] | None = None
    use_sigma_state: bool = False
    sigma_only: bool = False
    schema_version: Literal[1]
    strategy_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=200)]
    from_ms: Timestamp
    to_ms: Timestamp
    open_setups: Annotated[tuple[ForwardOpenSetup, ...], Field(max_length=2000)] = ()
    stage_run_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

    @model_validator(mode="after")
    def window(self):
        if self.to_ms < self.from_ms:
            raise ValueError("to_ms precedes from_ms")
        return self

    def evaluate_input(self):
        return {"symbol": self.symbol, "bars": [row.model_dump() for row in self.rows],
                "sigma_state": self.sigma_state, "use_sigma_state": self.use_sigma_state, "sigma_only": self.sigma_only,
                "funding": [{"calc_time_ms": f.calc_time_ms, "rate": str(f.rate),
                             "interval_hours": f.interval_hours,
                             **({"mark": str(f.mark)} if f.mark is not None else {})} for f in self.funding],
                "funding_available": self.funding_available, "strategy_ids": self.strategy_ids,
                "from_ms": self.from_ms, "to_ms": self.to_ms,
                "open_setups": [item.model_dump() for item in self.open_setups],
                "stage_run_id": self.stage_run_id}


class ForwardWalletEvent(InputModel):
    type: Literal["open", "close", "funding", "mark", "fill", "order", "tape", "cancel"]
    ms: Timestamp
    symbol: str = Field(min_length=5, max_length=16, pattern=r"^[A-Z0-9]+$")
    sequence: Annotated[int, Field(strict=True, ge=0)]
    stage_run_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    setup: dict[str, Any] | None = None
    setup_id: str | None = None
    outcome: str | None = None
    exit_ref_price: Annotated[int, Field(strict=True, gt=0)] | None = None
    rate: str | None = None
    mark: Annotated[int, Field(strict=True, gt=0)] | None = None
    # wallet-v3 only: a scale-in or partial exit (``fill``) at a reference ``price``
    fill_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    direction: Literal["increase", "reduce"] | None = None
    qty: str | None = None
    price: Annotated[int, Field(strict=True, gt=0)] | None = None
    liquidity: Literal["taker", "maker"] | None = None
    # wallet-v3 orders (``order``, ``cancel``) and one completed minute of trades (``tape``, stamped at its close)
    order_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    order_type: Literal["market", "limit"] | None = None
    limit_price: Annotated[int, Field(strict=True, gt=0)] | None = None
    expires_ms: Timestamp | None = None
    low: Annotated[int, Field(strict=True, gt=0)] | None = None
    high: Annotated[int, Field(strict=True, gt=0)] | None = None
    close: Annotated[int, Field(strict=True, gt=0)] | None = None
    through_qty: str | None = None

    @model_validator(mode="after")
    def required_payload(self):
        from fractions import Fraction
        if self.type == "open" and (not self.setup or self.setup.get("symbol") != self.symbol):
            raise ValueError("open requires a setup for the event symbol")
        if self.type == "close" and not self.setup_id:
            raise ValueError("exit requires setup_id")
        if self.type == "funding":
            if self.rate is None or self.mark is None:
                raise ValueError("funding requires rate and mark")
            try:
                rate = Fraction(self.rate)
            except (ValueError, ZeroDivisionError):
                raise ValueError("invalid funding rate") from None
            if abs(rate) > 1:
                raise ValueError("invalid funding rate")
        if self.type == "mark" and self.mark is None:
            raise ValueError("mark requires a mark price")
        if self.type == "fill":
            if not (self.setup_id and self.fill_id and self.direction and self.qty and self.price):
                raise ValueError("fill requires setup_id, fill_id, direction, qty and price")
            try:
                if Fraction(self.qty) <= 0:
                    raise ValueError
            except (ValueError, ZeroDivisionError):
                raise ValueError("invalid fill quantity") from None
        if self.type == "order":
            if not (self.order_id and self.order_type and self.setup and self.setup.get("symbol") == self.symbol):
                raise ValueError("order requires order_id, order_type and a setup for the event symbol")
            if self.order_type == "limit" and self.limit_price is None:
                raise ValueError("a limit order requires limit_price")
        if self.type == "cancel" and not self.order_id:
            raise ValueError("cancel requires order_id")
        if self.type == "tape":
            if None in (self.low, self.high, self.close) or not self.low <= self.close <= self.high:
                raise ValueError("tape requires low <= close <= high")
            if self.through_qty is not None:
                try:
                    if Fraction(self.through_qty) < 0:
                        raise ValueError
                except (ValueError, ZeroDivisionError):
                    raise ValueError("invalid through_qty") from None
        return self


class ForwardAdversePath(InputModel):
    setup_id: str
    symbol: str
    side: Literal[-1, 1]
    points: Annotated[tuple[tuple[Timestamp, Annotated[int, Field(strict=True, gt=0)]], ...], Field(max_length=200_000)]

    @model_validator(mode="after")
    def chronological(self):
        if any(b[0] <= a[0] or (b[1] >= a[1] if self.side == 1 else b[1] <= a[1])
               for a, b in zip(self.points, self.points[1:])):
            raise ValueError("adverse points must be chronological record extremes")
        return self


class ForwardReconcileInput(InputModel):
    """A whole ledger (lines carry ``seq``, ``type``, ``amount_e8``, ``balance_e8``) to reconcile with the state."""
    initial_balance_e8: Annotated[int, Field(strict=True)]
    ledger: Annotated[tuple[dict[str, Any], ...], Field(max_length=1_000_000)]


class ForwardWalletRequest(InputModel):
    schema_version: Literal[1]
    state: dict[str, Any] | None = None
    events: Annotated[tuple[ForwardWalletEvent, ...], Field(max_length=200_000)]
    config: ForwardWalletConfig | None = None
    adverse_paths: Annotated[tuple[ForwardAdversePath, ...], Field(max_length=200_000)] = ()
    reconcile: ForwardReconcileInput | None = None


class ForwardDailyBar(InputModel):
    """One completed UTC-day Binance kline as stored in ``forward_daily_bars`` (#239 P14)."""
    day_ms: Timestamp
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Nonnegative = Decimal(0)
    quote_volume: Nonnegative = Decimal(0)

    @model_validator(mode="after")
    def utc_day(self):
        if self.day_ms % 86_400_000:
            raise ValueError("day_ms must be 00:00 UTC")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent daily bar")
        return self


class ForwardTrendSymbol(InputModel):
    symbol: str = Field(min_length=5, max_length=16, pattern=r"^[A-Z0-9]+$")
    bars: Annotated[tuple[ForwardDailyBar, ...], Field(max_length=20000)]
    funding: Annotated[tuple[ForwardFundingEvent, ...], Field(max_length=60000)] = ()
    # The funding history the caller fetched covers (funding_from_ms, funding_to_ms]; a day outside it,
    # or any day when the fetch failed (funding_available false), is not finalised.
    funding_available: bool = True
    funding_from_ms: Timestamp | None = None
    funding_to_ms: Timestamp | None = None
    funding_interval_ms: int = 8 * 3_600_000


class ForwardTrendRequest(InputModel):
    schema_version: Literal[1]
    symbols: Annotated[tuple[ForwardTrendSymbol, ...], Field(min_length=1, max_length=20)]
    through_day_ms: Timestamp
    states: dict[str, dict[str, Any]] | None = None
    track_start_ms: Timestamp | None = None
    history_start_ms: Timestamp | None = None
    expected_symbols: tuple[str, ...] | None = None
    saved_params_hash: str | None = None
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    evaluated_at_ms: Timestamp  # supplied by the caller: the evaluator is pure (no wall clock)

    @model_validator(mode="after")
    def unique(self):
        if len({item.symbol for item in self.symbols}) != len(self.symbols):
            raise ValueError("duplicate symbol")
        return self

    def evaluate_input(self) -> dict:
        out = {"symbols": [{"symbol": item.symbol,
                            "bars": [{"day_ms": bar.day_ms, "open": str(bar.open), "close": str(bar.close)}
                                     for bar in item.bars],
                            "funding": [{"calc_time_ms": f.calc_time_ms, "rate": str(f.rate)} for f in item.funding],
                            "funding_available": item.funding_available, "funding_from_ms": item.funding_from_ms,
                            "funding_to_ms": item.funding_to_ms,
                            "funding_interval_ms": item.funding_interval_ms} for item in self.symbols],
               "through_day_ms": self.through_day_ms, "states": self.states}
        if self.track_start_ms is not None:
            out["track_start_ms"] = self.track_start_ms
        if self.history_start_ms is not None:
            out["history_start_ms"] = self.history_start_ms
        out.update(expected_symbols=self.expected_symbols, saved_params_hash=self.saved_params_hash,
                   decisions=self.decisions, evaluated_at_ms=self.evaluated_at_ms)
        return out


# ── Multi-hour movement descriptor ("Longer moves", slow-movement-v1) ─────────────────────────────

SlowGridPrice = Optional[Annotated[float, Field(gt=0, le=1e20)]]


class SlowMovementSymbolRequest(InputModel):
    symbol: str = Field(min_length=2, max_length=32, pattern=r"^[A-Z0-9]+$")
    grid: Annotated[list[SlowGridPrice], Field(min_length=672, max_length=672)]


class SlowMovementRequest(InputModel):
    """Stateless request for POST /v1/movement/slow: 672 grid prices per symbol (7 days at 15 minutes)."""

    boundaryMs: Timestamp
    horizonsMin: Annotated[list[Literal[60, 240, 720]], Field(min_length=1, max_length=3)]
    symbols: Annotated[list[SlowMovementSymbolRequest], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def aligned_unique(self):
        if self.boundaryMs % 900_000:
            raise ValueError("slow movement boundary must be a multiple of 15 minutes")
        if len(set(self.horizonsMin)) != len(self.horizonsMin):
            raise ValueError("slow movement horizons must be unique")
        names = [item.symbol for item in self.symbols]
        if len(names) != len(set(names)):
            raise ValueError("slow movement symbols must be unique")
        return self
