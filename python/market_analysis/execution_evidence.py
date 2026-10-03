"""Immutable #37 Part 2 evidence boundary; no clocks, fills or account state."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath
import re

from .canonical_identity import canonical_digest
from .futures_execution_contracts import ALGORITHM_VERSION, Evidence, Provenance, Scope
from .futures_execution import scope_reasons


EVIDENCE_VERSION = "binance-usdm-execution-evidence-v1"
SCHEMA_VERSION = "binance-usdm-execution-evidence-schema-v1"
EVIDENCE_POLICY = (
    "aggregate-only:no-fill-allocation:no-cross-stream-chronology:"
    "exact-event-funding-mark-only:one-minute-mark-envelope:explicit-applicability"
)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def sha256(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("canonical lowercase SHA-256 required")
    return value


def timestamp(value):
    if type(value) is not int or value < 0:
        raise ValueError("timestamp must be nonnegative integer epoch milliseconds")
    return value


def instrument(symbol, instrument_id):
    if (not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum()
            or symbol != symbol.upper() or not symbol.endswith("USDT") or len(symbol) <= 4
            or instrument_id != f"binance-usdm:{symbol}"):
        raise ValueError("symbol/instrument mismatch or unsupported USD-M instrument")


def scope(value):
    if not isinstance(value, Scope) or scope_reasons(value):
        raise ValueError("supported Part 1 execution scope required")


def decimal(value, name, *, positive=False, nonnegative=False):
    if (not isinstance(value, Decimal) or not value.is_finite()
            or positive and value <= 0 or nonnegative and value < 0):
        raise ValueError(f"{name} must be a finite permitted Decimal")
    return value


def relative_path(value):
    text(value, "relative source path")
    path = PurePosixPath(value)
    if (path.is_absolute() or ".." in path.parts or "\\" in value
            or PureWindowsPath(value).drive or str(path) != value):
        raise ValueError("canonical relative source path required")


class EvidenceIdentity:
    @property
    def identity(self):
        return canonical_digest({"evidence_version": EVIDENCE_VERSION,
                                 "schema_version": SCHEMA_VERSION,
                                 "math_version": ALGORITHM_VERSION,
                                 "policy": EVIDENCE_POLICY,
                                 "contract": type(self).__name__, "parameters": self})


class SourceKind(str, Enum):
    EXCHANGE_EVENT = "EXCHANGE_EVENT"
    CURRENT_SNAPSHOT = "CURRENT_SNAPSHOT"
    HISTORICAL_SNAPSHOT = "HISTORICAL_SNAPSHOT"
    FIXED_CONFIG = "FIXED_CONFIG"


@dataclass(frozen=True)
class SourceProvenance(EvidenceIdentity):
    source: str
    version: str
    classification: Provenance
    kind: SourceKind
    observed_at_ms: int | None = None
    effective_at_ms: int | None = None
    applicable_at_ms: int | None = None
    historical_valid_until_ms: int | None = None

    def __post_init__(self):
        text(self.source, "source")
        text(self.version, "source version")
        if not isinstance(self.classification, Provenance) or not isinstance(self.kind, SourceKind):
            raise ValueError("explicit provenance and source kind required")
        for value in (self.observed_at_ms, self.effective_at_ms, self.applicable_at_ms,
                      self.historical_valid_until_ms):
            if value is not None:
                timestamp(value)
        allowed = {
            SourceKind.EXCHANGE_EVENT: Provenance.ACTUAL_HISTORICAL,
            SourceKind.CURRENT_SNAPSHOT: Provenance.CURRENT_RULE_ASSUMPTION,
            SourceKind.HISTORICAL_SNAPSHOT: Provenance.ACTUAL_HISTORICAL,
            SourceKind.FIXED_CONFIG: Provenance.FIXED_SIMULATION_ASSUMPTION,
        }
        if self.classification not in (allowed[self.kind], Provenance.UNAVAILABLE):
            raise ValueError("source kind cannot masquerade as historical truth")
        if self.kind in (SourceKind.CURRENT_SNAPSHOT, SourceKind.HISTORICAL_SNAPSHOT):
            if self.observed_at_ms is None:
                raise ValueError("snapshot observation timestamp required")
        if (self.effective_at_ms is not None and self.historical_valid_until_ms is not None
                and self.historical_valid_until_ms < self.effective_at_ms):
            raise ValueError("historical applicability interval reversed")
        if (self.kind is SourceKind.HISTORICAL_SNAPSHOT
                and self.classification is Provenance.ACTUAL_HISTORICAL):
            if (self.effective_at_ms is None or self.applicable_at_ms is None
                    or self.historical_valid_until_ms is None
                    or not self.effective_at_ms <= self.applicable_at_ms <= self.historical_valid_until_ms):
                raise ValueError("historical snapshot requires supplied factual applicability interval")


@dataclass(frozen=True)
class SourceIdentity(EvidenceIdentity):
    provenance: SourceProvenance
    content_sha256: str

    def __post_init__(self):
        if not isinstance(self.provenance, SourceProvenance):
            raise ValueError("source provenance required")
        sha256(self.content_sha256)

    @property
    def part1(self):
        p = self.provenance
        # Bind the hash AND declared applicability to the evidence supplied to math.
        return Evidence(p.classification, p.source, f"{p.version}:{self.identity}",
                        str(p.effective_at_ms) if p.effective_at_ms is not None else None,
                        str(p.observed_at_ms) if p.observed_at_ms is not None else None)


@dataclass(frozen=True)
class EvidenceReference(EvidenceIdentity):
    instrument_id: str
    component: str
    evidence_identity: str | None = None
    unavailable_reason: str | None = "SOURCE_EVIDENCE_UNAVAILABLE"
    provenance: tuple[Provenance, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self):
        scope(Scope(self.instrument_id))
        text(self.component, "component")
        if (not isinstance(self.provenance, tuple)
                or any(not isinstance(p, Provenance) for p in self.provenance)
                or not isinstance(self.limitations, tuple)
                or any(not isinstance(v, str) or not v for v in self.limitations)):
            raise ValueError("immutable provenance and limitations required")
        if self.evidence_identity is None:
            text(self.unavailable_reason, "unavailable reason")
            if self.provenance:
                raise ValueError("missing component cannot claim factual provenance")
        else:
            sha256(self.evidence_identity)
            if self.unavailable_reason is not None or not self.provenance:
                raise ValueError("present evidence cannot also be missing")
        object.__setattr__(self, "provenance", tuple(sorted(set(self.provenance), key=lambda p: p.value)))
        object.__setattr__(self, "limitations", tuple(sorted(set(self.limitations))))


COMPONENTS = ("contract_rules", "brackets", "fees", "aggtrades", "mark_price", "funding")


@dataclass(frozen=True)
class ExecutionEvidenceSnapshot(EvidenceIdentity):
    scope: Scope
    components: tuple[EvidenceReference, ...]

    def __post_init__(self):
        scope(self.scope)
        if (not isinstance(self.components, tuple)
                or any(not isinstance(r, EvidenceReference) for r in self.components)
                or {r.component for r in self.components} != set(COMPONENTS)
                or len(self.components) != len(COMPONENTS)
                or any(r.instrument_id != self.scope.instrument_id for r in self.components)):
            raise ValueError("snapshot requires every component identity or explicit unavailable reason")
        object.__setattr__(self, "components", tuple(sorted(
            self.components, key=lambda r: COMPONENTS.index(r.component))))

    @property
    def snapshot_sha256(self):
        return self.identity


def execution_evidence_snapshot(execution_scope, *, contract_rules=None, brackets=None,
                                fees=None, aggtrades=None, mark_price=None, funding=None):
    """Bind typed component identities; omitted components remain explicitly missing."""
    # Local imports avoid coupling the neutral identity contracts to archive I/O.
    from .binance_execution_evidence import ExecutionTradeTapeEvidence, MarkRiskEvidence, FundingEvidence
    from .binance_execution_snapshots import ContractRuleSnapshot, BracketSnapshot, FeeSnapshot
    values = (contract_rules, brackets, fees, aggtrades, mark_price, funding)
    kinds = (ContractRuleSnapshot, BracketSnapshot, FeeSnapshot, ExecutionTradeTapeEvidence,
             MarkRiskEvidence, FundingEvidence)
    scope(execution_scope)
    references = []
    for component, value, kind in zip(COMPONENTS, values, kinds):
        classifications, limitations = (), ()
        if value is not None:
            if not isinstance(value, kind) or value.instrument_id != execution_scope.instrument_id:
                raise ValueError("component type/instrument mismatch")
            if hasattr(value, "scope") and value.scope != execution_scope:
                raise ValueError("component scope mismatch")
            classifications = (value.source.provenance.classification,) if hasattr(value, "source") else (Provenance.ACTUAL_HISTORICAL,)
            if component == "contract_rules":
                classifications += (value.reduce_only_policy_source.provenance.classification,)
            if component == "mark_price":
                limitations = ("ONE_MINUTE_OHLC", "EXACT_INTRAMINUTE_MARK_UNAVAILABLE",
                               "EXACT_INTRAMINUTE_TIMESTAMP_UNAVAILABLE", "NO_CROSS_STREAM_CHRONOLOGY")
                if not value.minutes:
                    limitations += ("MARK_MINUTES_UNAVAILABLE",)
            if component == "aggtrades":
                limitations = ("NO_PASSIVE_FILL_ALLOCATION", "NO_CROSS_STREAM_CHRONOLOGY")
                if not value.row_count:
                    limitations += ("TRADE_ROWS_UNAVAILABLE",)
            if component == "funding":
                limitations = tuple(f"UNAVAILABLE_FUNDING_MARK:{e.event_identity}"
                                    for e in value.events if e.exact_mark is None)
                limitations += tuple(f"FUNDING_MARK_OBSERVATION_TIME_UNAVAILABLE:{e.event_identity}"
                                     for e in value.events if e.exact_mark is not None
                                     and e.exact_mark.available_at_ms is None)
                if not value.events:
                    limitations += ("FUNDING_EVENTS_UNAVAILABLE",)
            if Provenance.UNAVAILABLE in classifications:
                limitations += ("SOURCE_PROVENANCE_UNAVAILABLE",)
        references.append(EvidenceReference(execution_scope.instrument_id, component,
                                           value.identity if value is not None else None,
                                           None if value is not None else "SOURCE_EVIDENCE_UNAVAILABLE",
                                           classifications, limitations))
    return ExecutionEvidenceSnapshot(execution_scope, tuple(references))
