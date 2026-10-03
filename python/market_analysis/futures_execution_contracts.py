"""Immutable explicit inputs for Binance USD-M execution math (#37 Part 1)."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .market_episode_lifecycle import _digest


ALGORITHM_VERSION = "binance-usdm-execution-math-v1:exact-decimal"
POLICY = (
    "base-unit-quantity:isolated-one-way:half-open-brackets:"
    "quantity-floor:passive-buy-floor-sell-ceil:explicit-adjustment:"
    "stop-buy-ge-sell-le:tp-buy-le-sell-ge:adverse-bps-10000"
)


class Provenance(str, Enum):
    ACTUAL_HISTORICAL = "ACTUAL_HISTORICAL"
    CURRENT_RULE_ASSUMPTION = "CURRENT_RULE_ASSUMPTION"
    FIXED_SIMULATION_ASSUMPTION = "FIXED_SIMULATION_ASSUMPTION"
    UNAVAILABLE = "UNAVAILABLE"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class FeeRole(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


class TriggerType(str, Enum):
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"


class PriceReference(str, Enum):
    CONTRACT_PRICE = "CONTRACT_PRICE"
    MARK_PRICE = "MARK_PRICE"


def number(value, name, *, minimum=None, positive=False):
    """Require Decimal, rejecting bool, float, NaN and infinities at the boundary."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")
    if positive and value <= 0 or minimum is not None and value < minimum:
        raise ValueError(f"{name} is outside its permitted range")
    return value


def enum_value(value, enum):
    if not isinstance(value, enum):
        raise ValueError(f"explicit {enum.__name__} required")
    return value


class Identified:
    @property
    def algorithm_version(self):
        return ALGORITHM_VERSION

    @property
    def identity(self):
        # Reuse the existing lossless domain serializer/hash; bind parameters,
        # metadata, contract type and fixed algorithm/policy semantics.
        return _digest({"algorithm": ALGORITHM_VERSION, "policy": POLICY,
                        "contract": type(self).__name__, "parameters": self})


@dataclass(frozen=True)
class Evidence(Identified):
    classification: Provenance
    source: str
    version: str
    effective_at: str | None = None
    observed_at: str | None = None

    def __post_init__(self):
        enum_value(self.classification, Provenance)
        for value in (self.source, self.version):
            if not isinstance(value, str) or not value:
                raise ValueError("source and version are required")
        for value in (self.effective_at, self.observed_at):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("time metadata must be explicit nonempty strings")


@dataclass(frozen=True)
class Scope(Identified):
    instrument_id: str
    settlement: str = "USDT"
    position_mode: str = "ONE_WAY"
    margin_mode: str = "ISOLATED"
    multi_assets: bool = False
    portfolio_margin: bool = False
    bnb_fee_state: bool = False
    real_trading: bool = False
    authenticated: bool = False

    def __post_init__(self):
        if not isinstance(self.instrument_id, str) or not self.instrument_id:
            raise ValueError("canonical instrument_id required")
        for name in ("settlement", "position_mode", "margin_mode"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be an explicit string")
        for name in ("multi_assets", "portfolio_margin", "bnb_fee_state",
                     "real_trading", "authenticated"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")


@dataclass(frozen=True)
class Grid(Identified):
    minimum: Decimal
    maximum: Decimal
    increment: Decimal
    evidence: Evidence
    origin: Decimal = Decimal(0)

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("grid evidence required")
        for name in ("minimum", "maximum", "increment", "origin"):
            number(getattr(self, name), name)


@dataclass(frozen=True)
class MinNotional(Identified):
    minimum: Decimal
    reduce_only_exempt: bool
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("minimum-notional evidence required")
        number(self.minimum, "minimum notional")
        if type(self.reduce_only_exempt) is not bool:
            raise ValueError("reduce_only_exempt must be boolean")


@dataclass(frozen=True)
class PercentPrice(Identified):
    multiplier_down: Decimal
    multiplier_up: Decimal
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("percent-price evidence required")
        number(self.multiplier_down, "multiplier_down")
        number(self.multiplier_up, "multiplier_up")


@dataclass(frozen=True)
class ContractRules(Identified):
    scope: Scope
    evidence: Evidence
    status: str | None
    contract_type: str | None
    price: Grid | None
    lot: Grid | None
    market_lot: Grid | None
    min_notional: MinNotional | None
    percent_price: PercentPrice | None = None
    percent_price_required: bool = False
    trigger_protect: Decimal | None = None

    def __post_init__(self):
        if not isinstance(self.scope, Scope) or not isinstance(self.evidence, Evidence):
            raise ValueError("scope and evidence required")
        for value in (self.price, self.lot, self.market_lot):
            if value is not None and not isinstance(value, Grid):
                raise ValueError("explicit Grid or None required")
        for value, kind in ((self.min_notional, MinNotional), (self.percent_price, PercentPrice)):
            if value is not None and not isinstance(value, kind):
                raise ValueError("explicit filter contract or None required")
        for value in (self.status, self.contract_type):
            if value is not None and not isinstance(value, str):
                raise ValueError("status and contract_type must be strings or None")
        if type(self.percent_price_required) is not bool:
            raise ValueError("percent_price_required must be boolean")
        if self.trigger_protect is not None:
            number(self.trigger_protect, "trigger_protect")


@dataclass(frozen=True)
class OrderIntent(Identified):
    side: OrderSide
    quantity: Decimal
    price: Decimal | None
    market: bool = False
    reduce_only: bool = False
    notional_price: Decimal | None = None
    percent_reference_price: Decimal | None = None

    def __post_init__(self):
        enum_value(self.side, OrderSide)
        number(self.quantity, "quantity", positive=True)
        for value in (self.price, self.notional_price, self.percent_reference_price):
            if value is not None:
                number(value, "price", positive=True)
        if type(self.market) is not bool or type(self.reduce_only) is not bool:
            raise ValueError("intent flags must be boolean")


@dataclass(frozen=True)
class FeePolicy(Identified):
    maker_rate: Decimal
    taker_rate: Decimal
    evidence: Evidence

    def __post_init__(self):
        number(self.maker_rate, "maker_rate", minimum=0)
        number(self.taker_rate, "taker_rate", minimum=0)
        if not isinstance(self.evidence, Evidence):
            raise ValueError("fee evidence required")


@dataclass(frozen=True)
class Bracket(Identified):
    bracket_id: str
    floor: Decimal
    cap: Decimal
    max_initial_leverage: Decimal
    maintenance_rate: Decimal
    cum: Decimal

    def __post_init__(self):
        if not isinstance(self.bracket_id, str):
            raise ValueError("bracket id must be a string")
        for name in ("floor", "cap", "max_initial_leverage", "maintenance_rate", "cum"):
            number(getattr(self, name), name)


@dataclass(frozen=True)
class BracketTable(Identified):
    scope: Scope
    rows: tuple[Bracket, ...]
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.scope, Scope) or not isinstance(self.evidence, Evidence):
            raise ValueError("scope and evidence required")
        if not isinstance(self.rows, tuple) or any(not isinstance(r, Bracket) for r in self.rows):
            raise ValueError("bracket rows must be an immutable tuple of Bracket")


@dataclass(frozen=True)
class Trigger(Identified):
    side: OrderSide
    kind: TriggerType
    reference: PriceReference
    price: Decimal
    price_protect: bool = False

    def __post_init__(self):
        enum_value(self.side, OrderSide)
        enum_value(self.kind, TriggerType)
        enum_value(self.reference, PriceReference)
        number(self.price, "trigger price", positive=True)
        if type(self.price_protect) is not bool:
            raise ValueError("price_protect must be boolean")


@dataclass(frozen=True)
class FixedBpsPolicy(Identified):
    bps: Decimal
    evidence: Evidence

    def __post_init__(self):
        number(self.bps, "bps", minimum=0)
        if not isinstance(self.evidence, Evidence):
            raise ValueError("fixed-bps evidence required")


@dataclass(frozen=True)
class Result(Identified):
    status: str
    reasons: tuple[str, ...]
    calculation: str
    inputs_identity: str
    value: Decimal | bool | None = None
    bracket_identity: str | None = None
    # Exact ratio is retained for divisions which have no finite Decimal value.
    numerator: Decimal | None = None
    denominator: Decimal | None = None


@dataclass(frozen=True)
class PositionCalculation(Identified):
    quantity: Decimal
    entry: Decimal | None
    realized_pnl: Decimal
    inputs_identity: str


@dataclass(frozen=True)
class Adjustment(Identified):
    original: Decimal
    proposed: Decimal
    policy: str
    grid_identity: str
    # Suggestions always require acceptance and full order revalidation later.
    requires_acceptance: bool = True
    status: str = "SUGGESTED"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class IncreaseCalculation(Identified):
    quantity: Decimal
    entry_result: Result
