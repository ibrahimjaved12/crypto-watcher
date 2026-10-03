"""Immutable explicit inputs for Binance USD-M execution math (#37 Part 1)."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .canonical_identity import canonical_digest
from .exact_scalar import ExactScalar, Scalar, exact_scalar


ALGORITHM_VERSION = "binance-usdm-execution-math-v4:exact-rational"
POLICY = (
    "base-unit-quantity:isolated-one-way:cap-inclusive-brackets:"
    "quantity-origin-min:quantity-floor:passive-buy-floor-sell-ceil:explicit-adjustment:"
    "stop-buy-ge-sell-le:tp-buy-le-sell-ge:adverse-bps-10000:exact-rational:"
    "percent-buy-upper-sell-lower-mark:market-min-notional-mark:"
    "price-filter-zero-disabled-origin-min:integer-leverage-1-125:market-no-price-limit-price-required:"
    "market-filter-market-lot-only:price-bearing-filter-lot-price-percent"
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
    """Require finite Decimal or exact scalar; never accept bool or floats."""
    try:
        exact = exact_scalar(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite Decimal or ExactScalar") from exc
    if positive and exact <= 0 or minimum is not None and exact < minimum:
        raise ValueError(f"{name} is outside its permitted range")
    return value


def leverage_value(value):
    if type(value) is not int or not 1 <= value <= 125:
        raise ValueError("Binance leverage must be an integer in [1, 125] (no bool)")
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
        return canonical_digest({"algorithm": ALGORITHM_VERSION, "policy": POLICY,
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
    """Binance quantity filter: minimum is the fixed lattice origin."""

    minimum: Scalar
    maximum: Scalar
    increment: Scalar
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("grid evidence required")
        for name in ("minimum", "maximum", "increment"):
            number(getattr(self, name), name)


@dataclass(frozen=True)
class PriceFilter(Identified):
    min_price: Scalar
    max_price: Scalar
    tick_size: Scalar
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("price-filter evidence required")
        for name in ("min_price", "max_price", "tick_size"):
            number(getattr(self, name), name)


@dataclass(frozen=True)
class RuleEvaluationContext(Identified):
    mark_price: Scalar | None
    evidence: Evidence

    def __post_init__(self):
        if not isinstance(self.evidence, Evidence):
            raise ValueError("mark evidence required")
        if self.mark_price is not None:
            number(self.mark_price, "mark_price", positive=True)


@dataclass(frozen=True)
class MinNotional(Identified):
    minimum: Scalar
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
    multiplier_down: Scalar
    multiplier_up: Scalar
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
    price: PriceFilter | None
    lot: Grid | None
    market_lot: Grid | None
    min_notional: MinNotional | None
    percent_price: PercentPrice | None = None
    percent_price_required: bool = False
    trigger_protect: Scalar | None = None

    def __post_init__(self):
        if not isinstance(self.scope, Scope) or not isinstance(self.evidence, Evidence):
            raise ValueError("scope and evidence required")
        if self.price is not None and not isinstance(self.price, PriceFilter):
            raise ValueError("explicit PriceFilter or None required")
        for value in (self.lot, self.market_lot):
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
    quantity: Scalar
    price: Scalar | None
    market: bool = False
    reduce_only: bool = False

    def __post_init__(self):
        enum_value(self.side, OrderSide)
        number(self.quantity, "quantity", positive=True)
        if self.price is not None:
            number(self.price, "price", positive=True)
        if type(self.market) is not bool or type(self.reduce_only) is not bool:
            raise ValueError("intent flags must be boolean")
        if self.market and self.price is not None:
            raise ValueError("MARKET intent must not contain a submitted price")
        if not self.market and self.price is None:
            raise ValueError("price-bearing intent requires a submitted price")


@dataclass(frozen=True)
class FeePolicy(Identified):
    maker_rate: Scalar
    taker_rate: Scalar
    evidence: Evidence

    def __post_init__(self):
        number(self.maker_rate, "maker_rate", minimum=0)
        number(self.taker_rate, "taker_rate", minimum=0)
        if not isinstance(self.evidence, Evidence):
            raise ValueError("fee evidence required")


@dataclass(frozen=True)
class Bracket(Identified):
    bracket_id: str
    floor: Scalar
    cap: Scalar
    max_initial_leverage: int
    maintenance_rate: Scalar
    cum: Scalar

    def __post_init__(self):
        if not isinstance(self.bracket_id, str):
            raise ValueError("bracket id must be a string")
        leverage_value(self.max_initial_leverage)
        for name in ("floor", "cap", "maintenance_rate", "cum"):
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
    price: Scalar
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
    bps: Scalar
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
    value: Scalar | bool | None = None
    bracket_identity: str | None = None

    @property
    def exact_value(self):
        return exact_scalar(self.value) if isinstance(self.value, (Decimal, ExactScalar)) else None

    @property
    def decimal_value(self):
        exact = self.exact_value
        return exact.decimal_value if exact is not None else None


@dataclass(frozen=True)
class PositionCalculation(Identified):
    quantity: Scalar
    entry: Scalar | None
    realized_pnl: Scalar
    inputs_identity: str


@dataclass(frozen=True)
class Adjustment(Identified):
    original: Scalar
    proposed: Scalar
    policy: str
    grid_identity: str
    # Suggestions always require acceptance and full order revalidation later.
    requires_acceptance: bool = True
    status: str = "SUGGESTED"
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class IncreaseCalculation(Identified):
    quantity: Scalar
    entry_result: Result
