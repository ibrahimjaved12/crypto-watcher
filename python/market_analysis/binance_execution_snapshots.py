"""Normalize explicitly supplied frozen JSON snapshots into Part 1 contracts.

No endpoint acquisition occurs here. Raw bytes, exact normalized parameters and
declared provenance all contribute to identity. No invalid intent is adjusted.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256 as hash_bytes
import json

from .canonical_identity import canonical_digest, canonical_value
from .exact_scalar import exact_scalar, finite_or_exact
from .execution_evidence import (
    EvidenceIdentity, SourceIdentity, SourceKind, SourceProvenance, decimal,
    instrument, scope, sha256, timestamp,
)
from .futures_execution import validate_brackets
from .futures_execution_contracts import (
    Bracket, BracketTable, ContractRules, FeePolicy, Grid, MinNotional,
    PercentPrice, PriceFilter, Provenance, Scope,
)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate snapshot JSON field: {key}")
        result[key] = value
    return result


def _constant(value):
    raise ValueError(f"nonfinite JSON number: {value}")


def _decode(raw):
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=_constant, object_pairs_hook=_pairs)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("valid frozen UTF-8 JSON required") from exc


def frozen_factual_json(raw):
    if not isinstance(raw, str):
        raise ValueError("immutable factual JSON string required")
    value = _decode(raw)
    if not isinstance(value, dict):
        raise ValueError("factual fields must be a JSON object")
    return json.dumps(canonical_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _snapshot(raw, provenance, expected_sha256=None):
    if not isinstance(raw, bytes) or not isinstance(provenance, SourceProvenance):
        raise ValueError("frozen snapshot bytes and explicit provenance required")
    digest = hash_bytes(raw).hexdigest()
    if expected_sha256 is not None and sha256(expected_sha256) != digest:
        raise ValueError("raw snapshot/content hash conflict")
    return _decode(raw), SourceIdentity(provenance, digest)


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} requires exact JSON numeric data")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"malformed {name}") from exc
    return decimal(result, name, positive=positive, nonnegative=nonnegative)


def _required_number(row, name, **options):
    if not isinstance(row, dict) or name not in row:
        raise ValueError(f"missing required snapshot field: {name}")
    return _number(row[name], name, **options)


def _symbol_row(payload, symbol, *, collection=None):
    if collection is not None:
        if not isinstance(payload, dict) or not isinstance(payload.get(collection), list):
            raise ValueError(f"snapshot requires {collection} array")
        rows = payload[collection]
    else:
        rows = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(row, dict) or not isinstance(row.get("symbol"), str) for row in rows):
        raise ValueError("snapshot symbol records required")
    if len({row["symbol"] for row in rows}) != len(rows):
        raise ValueError("duplicate snapshot symbol")
    matches = [row for row in rows if row["symbol"] == symbol]
    if len(matches) != 1:
        raise ValueError("snapshot symbol/instrument mismatch")
    return matches[0]


def _snapshot_kind(source):
    if source.provenance.kind is SourceKind.EXCHANGE_EVENT:
        raise ValueError("rule/bracket/fee snapshot provenance required")


@dataclass(frozen=True)
class ContractRuleSnapshot(EvidenceIdentity):
    symbol: str
    scope: Scope
    source: SourceIdentity
    rules: ContractRules
    reduce_only_policy_source: SourceIdentity

    def __post_init__(self):
        scope(self.scope)
        instrument(self.symbol, self.scope.instrument_id)
        if not isinstance(self.source, SourceIdentity) or not isinstance(self.rules, ContractRules):
            raise ValueError("normalized contract rules and source required")
        _snapshot_kind(self.source)
        r = self.rules
        if r.scope != self.scope or r.evidence != self.source.part1:
            raise ValueError("normalized rule scope/provenance mismatch")
        for name in ("price", "lot", "market_lot", "percent_price"):
            rule = getattr(r, name)
            if rule is None or rule.evidence != r.evidence:
                raise ValueError("all required normalized Binance filters must bind source")
        if (not isinstance(self.reduce_only_policy_source, SourceIdentity)
                or r.min_notional is None
                or r.min_notional.evidence != self.reduce_only_policy_source.part1):
            raise ValueError("minimum-notional policy must bind its explicit field provenance")
        _snapshot_kind(self.reduce_only_policy_source)
        if self.reduce_only_policy_source.content_sha256 != canonical_digest({
                "snapshot_source": self.source, "minimum": r.min_notional.minimum,
                "reduce_only_exempt": r.min_notional.reduce_only_exempt}):
            raise ValueError("minimum-notional policy content hash conflict")
        for grid in (r.lot, r.market_lot):
            if grid.minimum < 0 or grid.maximum <= 0 or grid.increment <= 0 or grid.maximum < grid.minimum:
                raise ValueError("malformed quantity filter")
        if (min(r.price.min_price, r.price.max_price, r.price.tick_size) < 0
                or r.price.max_price != 0 and r.price.min_price > r.price.max_price
                or r.min_notional.minimum < 0
                or not 0 < r.percent_price.multiplier_down <= 1 <= r.percent_price.multiplier_up
                or not r.percent_price_required
                or r.trigger_protect is not None and r.trigger_protect < 0):
            raise ValueError("malformed price/notional/percent filter")

    @property
    def instrument_id(self):
        return self.scope.instrument_id

    @property
    def normalized_identity(self):
        return self.rules.identity


def normalize_contract_snapshot(raw, symbol, provenance, *, reduce_only_exempt,
                                reduce_only_provenance=None, expected_sha256=None):
    """MIN_NOTIONAL reduce-only behavior is explicit; exchangeInfo omits it.

    If supplied by a simulation policy, use FIXED_CONFIG provenance. No exemption
    or historical effective timestamp is inferred from a current exchangeInfo file.
"""
    instrument(symbol, f"binance-usdm:{symbol}")
    if type(reduce_only_exempt) is not bool:
        raise ValueError("explicit minimum-notional reduce-only policy required")
    payload, source = _snapshot(raw, provenance, expected_sha256)
    row = _symbol_row(payload, symbol, collection="symbols")
    if (row.get("quoteAsset") != "USDT" or row.get("marginAsset") != "USDT"
            or not isinstance(row.get("status"), str) or not row["status"]
            or row.get("contractType") != "PERPETUAL"):
        raise ValueError("snapshot must describe a USDT-margined perpetual contract")
    filters = row.get("filters")
    if not isinstance(filters, list) or any(not isinstance(f, dict) or not isinstance(f.get("filterType"), str) for f in filters):
        raise ValueError("named exchange filters required")
    by_type = {f["filterType"]: f for f in filters}
    if len(by_type) != len(filters):
        raise ValueError("duplicate exchange filter")
    required = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "PERCENT_PRICE"}
    if not required <= by_type.keys():
        raise ValueError("missing required Binance contract filter")
    e = source.part1
    price = by_type["PRICE_FILTER"]
    pf = PriceFilter(*(_required_number(price, n, nonnegative=True) for n in ("minPrice", "maxPrice", "tickSize")), e)
    grids = []
    for kind in ("LOT_SIZE", "MARKET_LOT_SIZE"):
        f = by_type[kind]
        grids.append(Grid(_required_number(f, "minQty", nonnegative=True),
                          _required_number(f, "maxQty", positive=True),
                          _required_number(f, "stepSize", positive=True), e))
    minimum = by_type["MIN_NOTIONAL"]
    if "reduceOnlyExempt" in minimum and minimum["reduceOnlyExempt"] is not reduce_only_exempt:
        raise ValueError("contradictory reduce-only exemption")
    notional = _required_number(minimum, "notional", nonnegative=True)
    # exchangeInfo does not normally state reduce-only exemption. An explicit
    # caller policy must not inherit historical truth from the other filters.
    policy_provenance = reduce_only_provenance or SourceProvenance(
        "explicit-reduce-only-policy", "v1", Provenance.FIXED_SIMULATION_ASSUMPTION, SourceKind.FIXED_CONFIG)
    if "reduceOnlyExempt" in minimum and reduce_only_provenance is None:
        policy_provenance = provenance
    policy_source = SourceIdentity(policy_provenance, canonical_digest({
        "snapshot_source": source, "minimum": notional, "reduce_only_exempt": reduce_only_exempt}))
    mn = MinNotional(notional, reduce_only_exempt, policy_source.part1)
    percent = by_type["PERCENT_PRICE"]
    pp = PercentPrice(_required_number(percent, "multiplierDown", positive=True),
                      _required_number(percent, "multiplierUp", positive=True), e)
    execution_scope = Scope(f"binance-usdm:{symbol}")
    rules = ContractRules(execution_scope, e, row["status"], row["contractType"], pf,
                          *grids, mn, pp, True,
                          _required_number(row, "triggerProtect", nonnegative=True) if "triggerProtect" in row else None)
    return ContractRuleSnapshot(symbol, execution_scope, source, rules, policy_source)


@dataclass(frozen=True)
class BracketSnapshot(EvidenceIdentity):
    symbol: str
    scope: Scope
    source: SourceIdentity
    raw_brackets: tuple[Bracket, ...]
    table: BracketTable
    account_specific: bool
    notional_coef: Decimal | None
    values_basis: str

    def __post_init__(self):
        scope(self.scope)
        instrument(self.symbol, self.scope.instrument_id)
        if not isinstance(self.source, SourceIdentity) or not isinstance(self.table, BracketTable):
            raise ValueError("bracket source and normalized table required")
        _snapshot_kind(self.source)
        if type(self.account_specific) is not bool:
            raise ValueError("explicit account-specific flag required")
        if self.notional_coef is not None:
            decimal(self.notional_coef, "notionalCoef", positive=True)
            if not self.account_specific:
                raise ValueError("notionalCoef describes an account-specific adjustment")
        if (not isinstance(self.raw_brackets, tuple) or not self.raw_brackets
                or any(not isinstance(r, Bracket) for r in self.raw_brackets)):
            raise ValueError("immutable supplied brackets required")
        if self.values_basis not in ("BASE_TIERS", "EFFECTIVE_TIERS"):
            raise ValueError("explicit bracket values basis required")
        expected = _effective_brackets(self.raw_brackets, self.notional_coef, self.values_basis)
        if (self.table.scope != self.scope or self.table.evidence != self.source.part1
                or self.table.rows != expected):
            raise ValueError("normalized table must contain actual effective bracket values")
        # UNAVAILABLE provenance is still structurally validated, never made available to math.
        from dataclasses import replace
        evidence = replace(self.table.evidence, classification=Provenance.FIXED_SIMULATION_ASSUMPTION)
        if validate_brackets(replace(self.table, evidence=evidence)):
            raise ValueError("malformed or maintenance-discontinuous bracket table")

    @property
    def instrument_id(self):
        return self.scope.instrument_id

    @property
    def normalized_identity(self):
        return self.table.identity


def _effective_brackets(rows, coef, basis):
    multiplier = coef if coef is not None and basis == "BASE_TIERS" else Decimal(1)
    def scaled(value):
        return finite_or_exact(exact_scalar(value) * exact_scalar(multiplier))
    return tuple(Bracket(r.bracket_id, scaled(r.floor), scaled(r.cap), r.max_initial_leverage,
                         r.maintenance_rate, scaled(r.cum)) for r in rows)


def normalize_bracket_snapshot(raw, symbol, provenance, *, account_specific,
                               values_basis, expected_sha256=None):
    """Require an explicit base/effective declaration to avoid double scaling.

BASE_TIERS multiplies floor, cap AND cum by notionalCoef, retaining maintenance
continuity. EFFECTIVE_TIERS preserves already-adjusted supplied values exactly.
The declaration and coefficient are both identity-bound; neither is inferred.
"""
    instrument(symbol, f"binance-usdm:{symbol}")
    payload, source = _snapshot(raw, provenance, expected_sha256)
    row = _symbol_row(payload, symbol)
    brackets = row.get("brackets")
    if not isinstance(brackets, list) or not brackets:
        raise ValueError("nonempty supplied leverage brackets required")
    rows = []
    for tier in brackets:
        if (not isinstance(tier, dict) or type(tier.get("bracket")) is not int or tier["bracket"] <= 0
                or type(tier.get("initialLeverage")) is not int):
            raise ValueError("integer bracket ID and initial leverage required")
        rows.append(Bracket(str(tier["bracket"]), _required_number(tier, "notionalFloor", nonnegative=True),
                            _required_number(tier, "notionalCap", positive=True), tier["initialLeverage"],
                            _required_number(tier, "maintMarginRatio", nonnegative=True),
                            _required_number(tier, "cum", nonnegative=True)))
    raw_rows = tuple(rows)
    coef = _required_number(row, "notionalCoef", positive=True) if "notionalCoef" in row else None
    execution_scope = Scope(f"binance-usdm:{symbol}")
    table = BracketTable(execution_scope, _effective_brackets(raw_rows, coef, values_basis), source.part1)
    return BracketSnapshot(symbol, execution_scope, source, raw_rows, table, account_specific, coef, values_basis)


@dataclass(frozen=True)
class FeeSnapshot(EvidenceIdentity):
    symbol: str
    scope: Scope
    source: SourceIdentity
    policy: FeePolicy
    account_specific: bool

    def __post_init__(self):
        scope(self.scope)
        instrument(self.symbol, self.scope.instrument_id)
        if not isinstance(self.source, SourceIdentity) or not isinstance(self.policy, FeePolicy):
            raise ValueError("fee source and normalized policy required")
        _snapshot_kind(self.source)
        if type(self.account_specific) is not bool or self.policy.evidence != self.source.part1:
            raise ValueError("fee policy provenance/account flag mismatch")

    @property
    def instrument_id(self):
        return self.scope.instrument_id

    @property
    def normalized_identity(self):
        return self.policy.identity


def normalize_fee_snapshot(raw, symbol, provenance, *, account_specific, expected_sha256=None):
    """Frozen commission-rate or simulation config, without BNB wallet state."""
    instrument(symbol, f"binance-usdm:{symbol}")
    payload, source = _snapshot(raw, provenance, expected_sha256)
    row = _symbol_row(payload, symbol)
    policy = FeePolicy(_required_number(row, "makerCommissionRate", nonnegative=True),
                       _required_number(row, "takerCommissionRate", nonnegative=True), source.part1)
    return FeeSnapshot(symbol, Scope(f"binance-usdm:{symbol}"), source, policy, account_specific)


def settlement_mark_from_funding_history(raw, event, provenance, *, expected_sha256=None):
    """Join a frozen official /fapi/v1/fundingRate record to an existing event.

    Require unique agreement with every known event fact before enrichment.
    Missing rateType imposes no default; multiple matching events are ambiguous.
    Never infer interval from current fundingInfo; the archive event owns it.
    """
    from .binance_execution_evidence import (
        ExactSettlementMark, FundingSettlementEvidence, join_settlement_mark,
    )
    from dataclasses import replace
    if not isinstance(event, FundingSettlementEvidence):
        raise ValueError("exact funding event required")
    payload, source = _snapshot(raw, provenance, expected_sha256)
    rows = payload if isinstance(payload, list) else [payload]
    if any(not isinstance(r, dict) for r in rows):
        raise ValueError("funding-history records required")
    facts = _decode(event.factual_fields_json)
    matches = []
    for row in rows:
        if row.get("symbol") != event.symbol or row.get("fundingTime") != event.funding_timestamp_ms:
            continue
        timestamp(row["fundingTime"])
        if _required_number(row, "fundingRate") != event.funding_rate:
            continue
        if "fundingIntervalHours" in row and (type(row["fundingIntervalHours"]) is not int
                                            or row["fundingIntervalHours"] != event.funding_interval_hours):
            continue
        # Includes rateType only when already known. Every other known ancillary
        # fact must also be present and equal; markPrice never selects an event.
        if any(key not in row or canonical_value(row[key]) != value
               for key, value in facts.items() if key != "markPrice"):
            continue
        matches.append(row)
    if not matches:
        raise ValueError("funding-history symbol/time/rate/event facts mismatch")
    if len(matches) > 1:
        raise ValueError("ambiguous funding-history event: multiple matching records")
    row = matches[0]
    if "markPrice" in facts and ("markPrice" not in row or canonical_value(row["markPrice"]) != facts["markPrice"]):
        raise ValueError("funding-history mark fact mismatch")
    # Bind every supplied factual field (including rateType), retaining the original
    # archive event through its upstream/package identities. Unknown rateType is
    # enriched from the unique factual record, never assumed to mean Regular.
    ancillary = {k: v for k, v in row.items() if k not in ("symbol", "fundingTime", "fundingRate", "markPrice")}
    joined_facts = dict(facts, **ancillary)
    enriched = replace(event, factual_fields_json=json.dumps(canonical_value(joined_facts), sort_keys=True))
    mark = ExactSettlementMark(enriched.symbol, enriched.instrument_id, enriched.funding_timestamp_ms,
                               enriched.event_identity, _required_number(row, "markPrice", positive=True), source)
    return join_settlement_mark(enriched, mark)
