"""Versioned per-symbol contract inputs of the paper wallet (#36 Part 2).

``data/contract_inputs_v1.json`` holds, for the six frozen symbols, the maintenance-margin tiers (notional bracket,
rate, maintenance amount, max leverage), the taker/maker fee schedule, the liquidation fee rate and the tick, step
and minimum-notional filters, each with its source (``owner:#36``, ``cost-v1``, ``placeholder`` or ``unsupplied``).
Nothing is read from Binance: the signed leverage-bracket endpoint is not used.

A tier's ``floor < notional <= cap`` (an exact boundary belongs to the lower tier; zero belongs to the first tier,
as in ``futures_execution_contracts``). A notional above the last cap has no tier: it is unavailable, never
extrapolated. Maintenance amounts must keep ``notional * mmr - cum`` continuous at every edge (checked on load).
The hash is the SHA-256 of the file's canonical JSON, so line endings and whitespace do not change it.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
from pathlib import Path

from .. import data_lake

CONTRACT_INPUTS_VERSION = "contract-inputs-v1"
DATA_FILE = Path(__file__).parent / "data" / "contract_inputs_v1.json"
FROZEN_SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT")
OWNER_TIER = "owner-tier"
PLACEHOLDER_TIER = "placeholder-tier"
E8 = 10 ** 8


@dataclass(frozen=True)
class Tier:
    floor: Fraction  # USDT notional, exclusive lower edge (0 for the first tier: inclusive)
    cap: Fraction    # USDT notional, inclusive upper edge
    mmr: Fraction
    cum: Fraction    # maintenance amount, USDT
    max_leverage: int
    source: str

    @property
    def label(self) -> str:
        """The ledger assumption code of a line that uses this tier."""
        return OWNER_TIER if self.source.startswith("owner:") else PLACEHOLDER_TIER


@dataclass(frozen=True)
class SymbolInputs:
    symbol: str
    tiers: tuple
    taker_rate: Fraction
    maker_rate: Fraction
    liquidation_fee_rate: Fraction
    liquidation_fee_source: str
    tick_size: Fraction | None
    step_size: Fraction | None
    min_notional: Fraction | None

    def tier_for(self, notional_e8) -> Tier | None:
        """The bracket of a position notional (e8 USDT); None above the last cap."""
        notional = Fraction(notional_e8) / E8
        for index, tier in enumerate(self.tiers):
            if (notional <= tier.cap) and (notional > tier.floor or index == 0):
                return tier
        return None


@dataclass(frozen=True)
class ContractInputs:
    version: str
    sha256: str
    symbols: dict

    def symbol(self, name: str) -> SymbolInputs:
        if name not in self.symbols:
            raise ValueError(f"no contract inputs for {name}")
        return self.symbols[name]

    def __hash__(self) -> int:  # frozen dataclass with a dict field: identity is the file hash
        return hash(self.sha256)


def _fraction(text, what: str) -> Fraction:
    try:
        return Fraction(text)
    except (ValueError, ZeroDivisionError, TypeError):
        raise ValueError(f"invalid exact number for {what}: {text!r}") from None


def _optional(value, what: str) -> Fraction | None:
    return None if value is None else _fraction(value, what)


def _tiers(symbol: str, raw: list) -> tuple:
    tiers = []
    for index, item in enumerate(raw):
        tier = Tier(_fraction(item["floor"], f"{symbol} floor"), _fraction(item["cap"], f"{symbol} cap"),
                    _fraction(item["mmr"], f"{symbol} mmr"), _fraction(item["cum"], f"{symbol} cum"),
                    int(item["max_leverage"]), str(item["source"]))
        if not (0 <= tier.mmr < 1) or tier.cum < 0 or tier.max_leverage < 1 or tier.cap <= tier.floor:
            raise ValueError(f"{symbol} tier {index + 1} is invalid")
        if index == 0 and (tier.floor != 0 or tier.cum != 0):
            raise ValueError(f"{symbol} first tier must start at 0 with cum 0")
        if index:
            prior = tiers[-1]
            if tier.floor != prior.cap:
                raise ValueError(f"{symbol} tiers are not contiguous at tier {index + 1}")
            if tier.cum != prior.cum + prior.cap * (tier.mmr - prior.mmr):
                raise ValueError(f"{symbol} maintenance amount is not continuous at tier {index + 1}")
            if tier.max_leverage > prior.max_leverage or tier.mmr < prior.mmr:
                raise ValueError(f"{symbol} tiers must not loosen with notional")
        tiers.append(tier)
    if not tiers:
        raise ValueError(f"{symbol} has no tiers")
    return tuple(tiers)


def parse(document: dict) -> ContractInputs:
    if document.get("version") != CONTRACT_INPUTS_VERSION:
        raise ValueError(f"contract inputs version must be {CONTRACT_INPUTS_VERSION}")
    symbols = {}
    for name, raw in document["symbols"].items():
        filters = raw["filters"]
        symbols[name] = SymbolInputs(
            name, _tiers(name, raw["tiers"]), _fraction(raw["taker_rate"]["value"], f"{name} taker"),
            _fraction(raw["maker_rate"]["value"], f"{name} maker"),
            _fraction(raw["liquidation_fee_rate"]["value"], f"{name} liquidation fee"),
            str(raw["liquidation_fee_rate"]["source"]), _optional(filters["tick_size"], f"{name} tick"),
            _optional(filters["step_size"], f"{name} step"), _optional(filters["min_notional"], f"{name} min notional"))
    digest = hashlib.sha256(data_lake.canonical_json(document).encode("ascii")).hexdigest()
    return ContractInputs(document["version"], digest, symbols)


def load(path: Path = DATA_FILE) -> ContractInputs:
    return parse(json.loads(path.read_text(encoding="utf-8")))
