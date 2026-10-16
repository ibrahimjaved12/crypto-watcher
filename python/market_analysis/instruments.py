"""Market-agnostic instrument identity and strategy/data compatibility (#374).

Crypto keeps its existing ``binance-usdm:SYMBOL`` ids. Forex instruments use ``fx:SYMBOL``
(CFD-style spot) and ``fut:SYMBOL`` (futures). Ids are venue-independent; venue names live only
in ``VenueSymbol``. Mirrored by ``src/lib/market/instruments.ts`` (same reason strings).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
import re
from typing import FrozenSet, Optional, Tuple


class Market(str, Enum):
    """Top-level market an instrument trades in."""

    CRYPTO = "crypto"
    FOREX = "forex"


class InstrumentKind(str, Enum):
    """Contract kind; crypto is perpetual only, forex is CFD spot or futures."""

    PERPETUAL = "perpetual"
    SPOT_CFD = "spot_cfd"
    FUTURE = "future"


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    FX = "fx"
    METAL = "metal"
    ENERGY = "energy"
    INDEX = "index"


class DataField(str, Enum):
    """Data a venue provides and a strategy may require."""

    OHLC = "ohlc"
    VOLUME_REAL = "volume_real"
    VOLUME_TICK = "volume_tick"
    MARK = "mark"
    FUNDING = "funding"
    TAKER_FLOW = "taker_flow"
    OPEN_INTEREST = "open_interest"
    SPREAD = "spread"


CRYPTO_ID_PREFIX = "binance-usdm"
FOREX_SPOT_PREFIX = "fx"
FOREX_FUTURE_PREFIX = "fut"

_ID_PATTERN = re.compile(r"^[a-z0-9-]+:[A-Z0-9._-]+$")
_ALLOWED_KINDS = {
    Market.CRYPTO: frozenset({InstrumentKind.PERPETUAL}),
    Market.FOREX: frozenset({InstrumentKind.SPOT_CFD, InstrumentKind.FUTURE}),
}
_ID_PREFIX_BY_KIND = {
    InstrumentKind.PERPETUAL: CRYPTO_ID_PREFIX,
    InstrumentKind.SPOT_CFD: FOREX_SPOT_PREFIX,
    InstrumentKind.FUTURE: FOREX_FUTURE_PREFIX,
}


def _require_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


@dataclass(frozen=True)
class Instrument:
    """Venue-independent tradable instrument."""

    instrument_id: str
    market: Market
    kind: InstrumentKind
    asset_class: AssetClass
    base: str
    quote: str
    display: str

    def __post_init__(self) -> None:
        for name in ("instrument_id", "base", "quote", "display"):
            _require_text(name, getattr(self, name))
        # Accept raw strings (e.g. from storage) and normalise them to the enums.
        object.__setattr__(self, "market", Market(self.market))
        object.__setattr__(self, "kind", InstrumentKind(self.kind))
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        if not _ID_PATTERN.match(self.instrument_id):
            raise ValueError(
                f"instrument_id {self.instrument_id!r} must look like 'prefix:SYMBOL' "
                "(lowercase prefix, uppercase symbol)")
        if self.kind not in _ALLOWED_KINDS[self.market]:
            allowed = ", ".join(sorted(k.value for k in _ALLOWED_KINDS[self.market]))
            raise ValueError(
                f"kind {self.kind.value!r} is not valid for market {self.market.value!r} "
                f"(allowed: {allowed})")
        prefix = _ID_PREFIX_BY_KIND[self.kind]
        if self.instrument_id.split(":", 1)[0] != prefix:
            raise ValueError(
                f"instrument_id {self.instrument_id!r} must start with '{prefix}:' "
                f"for kind {self.kind.value!r}")


@dataclass(frozen=True)
class VenueSymbol:
    """How one venue names an instrument; contract details come from the broker, never guessed."""

    venue: str
    native_symbol: str
    instrument_id: str
    digits: Optional[int] = None
    tick_size: Optional[Fraction] = None
    contract_size: Optional[Fraction] = None
    expiry_ms: Optional[int] = None

    def __post_init__(self) -> None:
        for name in ("venue", "native_symbol", "instrument_id"):
            _require_text(name, getattr(self, name))
        if not _ID_PATTERN.match(self.instrument_id):
            raise ValueError(f"instrument_id {self.instrument_id!r} must look like 'prefix:SYMBOL'")
        if self.digits is not None and (
                isinstance(self.digits, bool) or not isinstance(self.digits, int) or self.digits < 0):
            raise ValueError("digits must be an integer >= 0")
        for name in ("tick_size", "contract_size"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, Fraction)) or value <= 0:
                raise ValueError(f"{name} must be a positive Fraction")
            object.__setattr__(self, name, Fraction(value))
        if self.expiry_ms is not None and (
                isinstance(self.expiry_ms, bool) or not isinstance(self.expiry_ms, int)
                or self.expiry_ms <= 0):
            raise ValueError("expiry_ms must be a positive UTC epoch millisecond integer")


def crypto_instrument(symbol: str) -> Instrument:
    """Binance USDⓈ-M USDT perpetual; the id format is unchanged (``binance-usdm:SYMBOL``)."""
    _require_text("symbol", symbol)
    native = symbol.strip().upper()
    if not native.endswith("USDT") or len(native) <= len("USDT"):
        raise ValueError(f"crypto symbol {symbol!r} must be a USDT perpetual like 'BTCUSDT'")
    return Instrument(
        instrument_id=f"{CRYPTO_ID_PREFIX}:{native}", market=Market.CRYPTO,
        kind=InstrumentKind.PERPETUAL, asset_class=AssetClass.CRYPTO,
        base=native[:-len("USDT")], quote="USDT", display=native)


def forex_instrument(symbol: str, kind: InstrumentKind = InstrumentKind.SPOT_CFD,
                     asset_class: AssetClass = AssetClass.FX, base: Optional[str] = None,
                     quote: Optional[str] = None, display: Optional[str] = None) -> Instrument:
    """Forex instrument; base/quote default to 3+3 letters only for a six-letter pair symbol."""
    _require_text("symbol", symbol)
    native = symbol.strip().upper()
    kind = InstrumentKind(kind)
    if kind not in _ALLOWED_KINDS[Market.FOREX]:
        raise ValueError(f"kind {kind.value!r} is not valid for market 'forex'")
    if len(native) == 6 and native.isalpha():
        base = base or native[:3]
        quote = quote or native[3:]
    if not base or not quote:
        raise ValueError(
            f"forex symbol {symbol!r} is not a six-letter pair: base and quote are required")
    return Instrument(
        instrument_id=f"{_ID_PREFIX_BY_KIND[kind]}:{native}", market=Market.FOREX, kind=kind,
        asset_class=AssetClass(asset_class), base=base.upper(), quote=quote.upper(),
        display=display or native)


FOREX_DEFAULTS: Tuple[Instrument, ...] = (
    forex_instrument("XAUUSD", asset_class=AssetClass.METAL, base="XAU", quote="USD",
                     display="Gold"),
    forex_instrument("EURUSD"),
    forex_instrument("USDJPY"),
    forex_instrument("USOIL", asset_class=AssetClass.ENERGY, base="USOIL", quote="USD",
                     display="WTI crude"),
)

FOREX_OPTIONAL: Tuple[Instrument, ...] = (
    forex_instrument("DXY", asset_class=AssetClass.INDEX, base="DXY", quote="USD",
                     display="US Dollar Index"),
    forex_instrument("UKOIL", asset_class=AssetClass.ENERGY, base="UKOIL", quote="USD",
                     display="Brent crude"),
)

CRYPTO_FIELDS: FrozenSet[DataField] = frozenset({
    DataField.OHLC, DataField.VOLUME_REAL, DataField.MARK, DataField.FUNDING,
    DataField.TAKER_FLOW, DataField.OPEN_INTEREST})
FOREX_FIELDS: FrozenSet[DataField] = frozenset({
    DataField.OHLC, DataField.VOLUME_TICK, DataField.SPREAD})


def provided_fields(instrument: Instrument) -> FrozenSet[DataField]:
    """Fields the instrument's market provides; a futures venue also has real traded volume."""
    if instrument.market is Market.CRYPTO:
        return CRYPTO_FIELDS
    if instrument.kind is InstrumentKind.FUTURE:
        return FOREX_FIELDS | {DataField.VOLUME_REAL}
    return FOREX_FIELDS


@dataclass(frozen=True)
class StrategyCapabilities:
    """What a strategy needs; ``requires_any`` groups each need at least one member present."""

    markets: FrozenSet[Market]
    requires: FrozenSet[DataField] = frozenset()
    requires_any: Tuple[FrozenSet[DataField], ...] = ()
    session_aware: bool = False

    def __post_init__(self) -> None:
        markets = frozenset(Market(m) for m in self.markets)
        if not markets:
            raise ValueError("markets must not be empty")
        groups = tuple(frozenset(DataField(f) for f in group) for group in self.requires_any)
        if any(not group for group in groups):
            raise ValueError("requires_any groups must not be empty")
        object.__setattr__(self, "markets", markets)
        object.__setattr__(self, "requires", frozenset(DataField(f) for f in self.requires))
        object.__setattr__(self, "requires_any", groups)


@dataclass(frozen=True)
class Compatibility:
    ok: bool
    reasons: Tuple[str, ...]


def check_compatibility(caps: Optional[StrategyCapabilities], instrument: Instrument,
                        provided: Optional[FrozenSet[DataField]] = None) -> Compatibility:
    """Fail closed. Reason order: no capabilities, market, missing fields, any-of groups."""
    if caps is None:
        return Compatibility(False, ("no_capabilities_declared",))
    reasons = []
    if instrument.market not in caps.markets:
        reasons.append(f"market_not_supported:{instrument.market.value}")
    have = provided_fields(instrument) if provided is None else frozenset(provided)
    reasons.extend(f"missing_field:{name}"
                   for name in sorted(f.value for f in caps.requires - have))
    for group in caps.requires_any:
        if not group & have:
            reasons.append("missing_any_of:" + "|".join(sorted(f.value for f in group)))
    return Compatibility(not reasons, tuple(reasons))


__all__ = [
    "AssetClass",
    "CRYPTO_FIELDS",
    "Compatibility",
    "DataField",
    "FOREX_DEFAULTS",
    "FOREX_FIELDS",
    "FOREX_OPTIONAL",
    "Instrument",
    "InstrumentKind",
    "Market",
    "StrategyCapabilities",
    "VenueSymbol",
    "check_compatibility",
    "crypto_instrument",
    "forex_instrument",
    "provided_fields",
]
