"""Immutable #37 Part 3 input views and proposals; #36 owns all committed state."""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .canonical_identity import canonical_digest
from .execution_evidence import ExecutionEvidenceSnapshot, scope, sha256, text, timestamp
from .futures_execution_contracts import (
    ALGORITHM_VERSION as MATH_VERSION, ContractRules, Evidence, FeeRole, FixedBpsPolicy,
    OrderIntent, OrderSide, PositionSide, Provenance, Result, RuleEvaluationContext,
    Scope, Trigger, enum_value, number,
)
from .exact_scalar import Scalar, exact_scalar


ALGORITHM_VERSION = "binance-usdm-execution-resolution-v2"
POLICY = (
    "next-eligible-contract-trade:market-full-fill:spread-then-slippage:taker:"
    "passive-opposite-aggressor-strict-through:full-print-volume-cap:limit-price:maker:"
    "valid-admission-only:child-admit-at-activation:same-trigger-event-excluded:"
    "external-same-time-unresolved:mark-crossing-interval:preexisting-open-condition-bounds-from-activation:"
    "mark-child-active-after-conservative-bound:"
    "trigger-occurrence-separate-from-child-admission:stable-selected-frontier-closeout-proof:"
    "causal-partial-order:no-event-type-priority:explicit-competing-frontier-ambiguity:"
    "event-funding:exact-settlement-mark:confirmed-position-applicability:"
    "risk-threshold-distinct-from-fixed-bps-closeout:separate-closeout-charge:"
    "bounded-trade-iterator:no-wallet-or-ledger"
)
MAX_FRONTIER_CANDIDATES = 128


class ResolutionIdentity:
    @property
    def identity(self):
        return canonical_digest({"algorithm": ALGORITHM_VERSION, "math": MATH_VERSION,
                                 "policy": POLICY, "contract": type(self).__name__, "parameters": self})

    @property
    def proposal_id(self):
        return self.identity


def _hashes(values):
    if not isinstance(values, tuple):
        raise ValueError("immutable identity tuple required")
    for value in values:
        sha256(value)


@dataclass(frozen=True)
class CausalBounds(ResolutionIdentity):
    earliest_possible_ms: int
    latest_possible_ms: int
    stream_id: str
    source_event_identity: str
    sequence_key: tuple[int, int] | None = None

    def __post_init__(self):
        timestamp(self.earliest_possible_ms)
        timestamp(self.latest_possible_ms)
        text(self.stream_id, "source stream")
        sha256(self.source_event_identity)
        if self.latest_possible_ms < self.earliest_possible_ms:
            raise ValueError("reversed causal bounds")
        if self.sequence_key is not None:
            if (not isinstance(self.sequence_key, tuple) or len(self.sequence_key) != 2
                    or not self.stream_id.startswith("aggTrades:")):
                raise ValueError("only the explicit aggTrade stream supplies sequence keys")
            scope(Scope(self.stream_id.removeprefix("aggTrades:")))
            for value in self.sequence_key:
                timestamp(value)
            if not self.exact or self.sequence_key[0] != self.earliest_possible_ms:
                raise ValueError("sequence key must bind its exact event timestamp")

    @property
    def exact(self):
        return self.earliest_possible_ms == self.latest_possible_ms


@dataclass(frozen=True)
class CausalPolicy(ResolutionIdentity):
    version: str = "DISJOINT_INTERVAL_OR_SAME_STREAM_SEQUENCE_V1"

    def __post_init__(self):
        if self.version != "DISJOINT_INTERVAL_OR_SAME_STREAM_SEQUENCE_V1":
            raise ValueError("unsupported causal policy")


@dataclass(frozen=True)
class AmbiguityPolicy(ResolutionIdentity):
    version: str = "EXPLICIT_AMBIGUITY_V1"

    def __post_init__(self):
        if self.version != "EXPLICIT_AMBIGUITY_V1":
            raise ValueError("unsupported ambiguity policy")


SIMULATION_EVIDENCE = Evidence(Provenance.FIXED_SIMULATION_ASSUMPTION,
                               "explicit-execution-resolution-assumption", ALGORITHM_VERSION)


@dataclass(frozen=True)
class FillPolicy(ResolutionIdentity):
    spread: FixedBpsPolicy
    slippage: FixedBpsPolicy
    evidence: Evidence = SIMULATION_EVIDENCE
    market_model: str = "NEXT_ELIGIBLE_CONTRACT_TRADE_FULL_FILL_V1"
    passive_model: str = "PASSIVE_TRADE_THROUGH_FULL_PRINT_CAP_V1"

    def __post_init__(self):
        if not isinstance(self.spread, FixedBpsPolicy) or not isinstance(self.slippage, FixedBpsPolicy):
            raise ValueError("separate explicit spread and slippage policies required")
        if (not isinstance(self.evidence, Evidence)
                or self.evidence.classification not in (Provenance.FIXED_SIMULATION_ASSUMPTION, Provenance.UNAVAILABLE)):
            raise ValueError("fill model is an explicit simulation assumption")
        if (self.market_model != "NEXT_ELIGIBLE_CONTRACT_TRADE_FULL_FILL_V1"
                or self.passive_model != "PASSIVE_TRADE_THROUGH_FULL_PRINT_CAP_V1"):
            raise ValueError("unsupported fill model")


@dataclass(frozen=True)
class CloseoutPolicy(ResolutionIdentity):
    adverse_bps: FixedBpsPolicy
    fee_rate: Scalar
    fee_evidence: Evidence
    evidence: Evidence = SIMULATION_EVIDENCE
    version: str = "FIXED_BPS_FROM_LIQUIDATION_THRESHOLD_V1"

    def __post_init__(self):
        if not isinstance(self.adverse_bps, FixedBpsPolicy) or not isinstance(self.fee_evidence, Evidence):
            raise ValueError("explicit closeout bps and charge evidence required")
        number(self.fee_rate, "closeout fee rate", minimum=0)
        if (not isinstance(self.evidence, Evidence)
                or self.evidence.classification not in (Provenance.FIXED_SIMULATION_ASSUMPTION, Provenance.UNAVAILABLE)
                or self.version != "FIXED_BPS_FROM_LIQUIDATION_THRESHOLD_V1"):
            raise ValueError("closeout is a versioned simulation approximation")


@dataclass(frozen=True)
class AdmittedOrder(ResolutionIdentity):
    order_id: str
    position_id: str
    scope: Scope
    intent: OrderIntent
    rules: ContractRules
    rule_input_identity: str
    validation: Result
    context: RuleEvaluationContext | None
    evidence: ExecutionEvidenceSnapshot
    activation: CausalBounds
    conditional_parent_identity: str | None = None

    def __post_init__(self):
        text(self.order_id, "order_id")
        text(self.position_id, "position_id")
        scope(self.scope)
        sha256(self.rule_input_identity)
        if (not isinstance(self.intent, OrderIntent) or not isinstance(self.rules, ContractRules)
                or self.rules.scope != self.scope or not isinstance(self.validation, Result)
                or self.validation.status != "VALID" or self.validation.calculation != "validate_order"
                or self.validation.inputs_identity != canonical_digest((self.intent, self.rules, self.context))):
            raise ValueError("admission must bind a VALID Part 1 validation and its exact inputs")
        if self.context is not None and not isinstance(self.context, RuleEvaluationContext):
            raise ValueError("exact explicit rule context required")
        # Validate when constructing an admission proof, never while evaluating
        # subsequent fills or immutable remaining-quantity views.
        from .futures_execution import validate_order
        if self.validation != validate_order(self.intent, self.rules, self.context):
            raise ValueError("admission proof must equal the actual Part 1 validation result")
        if (not isinstance(self.evidence, ExecutionEvidenceSnapshot) or self.evidence.scope != self.scope
                or not isinstance(self.activation, CausalBounds)):
            raise ValueError("admission evidence/scope/activation mismatch")
        if self.activation.sequence_key is not None and self.activation.stream_id != f"aggTrades:{self.scope.instrument_id}":
            raise ValueError("admission activation stream instrument mismatch")
        rule_ref = next(r for r in self.evidence.components if r.component == "contract_rules")
        if rule_ref.evidence_identity != self.rule_input_identity:
            raise ValueError("contract-rule evidence reference mismatch")
        if self.conditional_parent_identity is not None:
            sha256(self.conditional_parent_identity)

    @property
    def contract_rule_identity(self):
        return self.rules.identity

    @property
    def validation_identity(self):
        return self.validation.identity

    @property
    def admission_timestamp_ms(self):
        return self.activation.latest_possible_ms


@dataclass(frozen=True)
class AdmissionResult(ResolutionIdentity):
    validation: Result
    admitted: AdmittedOrder | None
    input_identity: str

    def __post_init__(self):
        sha256(self.input_identity)
        if not isinstance(self.validation, Result):
            raise ValueError("Part 1 validation result required")
        if (self.validation.status == "VALID") != (self.admitted is not None):
            raise ValueError("only VALID results admit executable orders")
        if self.admitted is not None and (not isinstance(self.admitted, AdmittedOrder)
                                         or self.admitted.validation != self.validation):
            raise ValueError("admission/result mismatch")

    @property
    def status(self):
        return self.validation.status


class OrderState(str, Enum):
    ACTIVE = "ACTIVE"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class OrderExecutionView(ResolutionIdentity):
    admitted: AdmittedOrder
    remaining_quantity: Scalar
    state: OrderState = OrderState.ACTIVE
    last_fill_event: CausalBounds | None = None
    caller_state_identity: str | None = None

    def __post_init__(self):
        if not isinstance(self.admitted, AdmittedOrder):
            raise ValueError("valid admitted order required")
        enum_value(self.state, OrderState)
        number(self.remaining_quantity, "remaining_quantity", minimum=0)
        if self.remaining_quantity > self.admitted.intent.quantity:
            raise ValueError("remaining quantity exceeds admitted quantity")
        if self.state is OrderState.ACTIVE and self.remaining_quantity != self.admitted.intent.quantity:
            raise ValueError("partially filled view requires explicit PARTIAL lifecycle")
        if self.state is OrderState.PARTIAL and not 0 < self.remaining_quantity < self.admitted.intent.quantity:
            raise ValueError("PARTIAL view requires positive reduced remaining quantity")
        if self.state is OrderState.FILLED and self.remaining_quantity != 0:
            raise ValueError("FILLED view must have zero remaining quantity")
        if self.state in (OrderState.ACTIVE, OrderState.PARTIAL) and self.remaining_quantity <= 0:
            raise ValueError("executable view requires positive remaining quantity")
        if self.remaining_quantity < self.admitted.intent.quantity and self.last_fill_event is None:
            raise ValueError("reduced remaining quantity requires the committed fill cursor")
        if self.last_fill_event is not None:
            if (not isinstance(self.last_fill_event, CausalBounds)
                    or self.last_fill_event.sequence_key is None
                    or self.last_fill_event.stream_id != f"aggTrades:{self.admitted.scope.instrument_id}"):
                raise ValueError("fill cursor must bind the instrument's exact aggTrade key")
        if self.caller_state_identity is not None:
            sha256(self.caller_state_identity)


@dataclass(frozen=True)
class PositionExecutionView(ResolutionIdentity):
    position_id: str
    scope: Scope
    side: PositionSide
    quantity: Scalar
    average_entry: Scalar
    isolated_wallet_collateral: Scalar
    effective_from_ms: int
    effective_through_ms: int | None = None
    caller_state_identity: str | None = None

    def __post_init__(self):
        text(self.position_id, "position_id")
        scope(self.scope)
        enum_value(self.side, PositionSide)
        number(self.quantity, "position quantity", minimum=0)
        number(self.average_entry, "average_entry", positive=True)
        number(self.isolated_wallet_collateral, "isolated_wallet_collateral", minimum=0)
        timestamp(self.effective_from_ms)
        if self.effective_through_ms is not None:
            timestamp(self.effective_through_ms)
            if self.effective_through_ms < self.effective_from_ms:
                raise ValueError("position applicability interval reversed")
        if self.caller_state_identity is not None:
            sha256(self.caller_state_identity)

    def covers(self, earliest, latest):
        return (self.effective_through_ms is not None
                and self.effective_from_ms <= earliest <= latest <= self.effective_through_ms)


@dataclass(frozen=True)
class ConditionalOrder(ResolutionIdentity):
    order_id: str
    child_order_id: str
    position_id: str
    scope: Scope
    trigger: Trigger
    child_intent: OrderIntent
    creation: CausalBounds
    evidence_identity: str

    def __post_init__(self):
        for value in (self.order_id, self.child_order_id, self.position_id):
            text(value, "conditional order identifier")
        if self.order_id == self.child_order_id:
            raise ValueError("parent and child require distinct stable order IDs")
        scope(self.scope)
        if (not isinstance(self.trigger, Trigger) or not isinstance(self.child_intent, OrderIntent)
                or self.trigger.side != self.child_intent.side or not isinstance(self.creation, CausalBounds)):
            raise ValueError("explicit same-side trigger/child intent and creation boundary required")
        sha256(self.evidence_identity)


@dataclass(frozen=True)
class ConditionalOrderView(ResolutionIdentity):
    order: ConditionalOrder
    state: str = "WAITING_TRIGGER"
    caller_state_identity: str | None = None

    def __post_init__(self):
        if not isinstance(self.order, ConditionalOrder) or self.state not in (
                "WAITING_TRIGGER", "TRIGGERED_CHILD_REJECTED", "TRIGGERED_CHILD_UNAVAILABLE",
                "ACTIVE", "CANCELLED", "FILLED"):
            raise ValueError("explicit conditional lifecycle view required")
        if self.caller_state_identity is not None:
            sha256(self.caller_state_identity)


@dataclass(frozen=True)
class ProposalBinding(ResolutionIdentity):
    input_identity: str
    evidence_identity: str
    source_evidence_identity: str
    causal: CausalBounds
    policy_identities: tuple[str, ...]

    def __post_init__(self):
        for value in (self.input_identity, self.evidence_identity, self.source_evidence_identity):
            sha256(value)
        if not isinstance(self.causal, CausalBounds):
            raise ValueError("explicit causal bounds required")
        _hashes(self.policy_identities)


@dataclass(frozen=True)
class UnavailableProposal(ResolutionIdentity):
    binding: ProposalBinding
    status: str
    reasons: tuple[str, ...]
    calculation: Result | None = None
    related_event_identities: tuple[str, ...] = ()
    kind: str = "UNAVAILABLE"

    def __post_init__(self):
        if not isinstance(self.binding, ProposalBinding) or not isinstance(self.reasons, tuple) or not self.reasons:
            raise ValueError("unavailable proposal requires binding and explicit reasons")
        text(self.status, "unavailable status")
        for reason in self.reasons:
            text(reason, "unavailable reason")
        _hashes(self.related_event_identities)
        if self.calculation is not None and not isinstance(self.calculation, Result):
            raise ValueError("explicit Part 1 calculation required")
        if self.kind != "UNAVAILABLE":
            raise ValueError("unavailable proposal kind mismatch")


@dataclass(frozen=True)
class FillProposal(ResolutionIdentity):
    binding: ProposalBinding
    order_id: str
    admission_identity: str
    scope: Scope
    side: OrderSide
    requested_quantity: Scalar
    remaining_before: Scalar
    fill_quantity: Scalar
    remaining_after: Scalar
    submitted_limit_price: Scalar | None
    anchor_contract_price: Scalar
    after_spread_price: Scalar | None
    fill_price: Scalar
    fee_role: FeeRole
    normal_trading_fee: Result
    spread_policy_identity: str | None
    slippage_policy_identity: str | None
    execution_policy_identity: str
    status: str
    next_state: OrderState
    classification: Provenance = Provenance.FIXED_SIMULATION_ASSUMPTION
    kind: str = "FILL_PROPOSAL"

    def __post_init__(self):
        scope(self.scope)
        text(self.order_id, "order_id")
        for value in (self.admission_identity, self.execution_policy_identity):
            sha256(value)
        if not isinstance(self.binding, ProposalBinding) or self.binding.causal.sequence_key is None:
            raise ValueError("fill must bind an exact source aggTrade")
        enum_value(self.side, OrderSide)
        enum_value(self.fee_role, FeeRole)
        enum_value(self.next_state, OrderState)
        for value in (self.requested_quantity, self.remaining_before, self.fill_quantity,
                      self.anchor_contract_price, self.fill_price):
            number(value, "fill input/output", positive=True)
        number(self.remaining_after, "remaining_after", minimum=0)
        if (self.remaining_before > self.requested_quantity
                or exact_scalar(self.fill_quantity) + exact_scalar(self.remaining_after) != self.remaining_before
                or self.status != ("FILLED" if self.remaining_after == 0 else "PARTIAL")
                or self.next_state.value != self.status):
            raise ValueError("fill/remaining/lifecycle inconsistency")
        if (not isinstance(self.normal_trading_fee, Result) or self.normal_trading_fee.status != "VALID"
                or self.normal_trading_fee.calculation != "fee"):
            raise ValueError("fill requires available normal trading fee")
        if self.submitted_limit_price is None:
            if self.fee_role is not FeeRole.TAKER or self.after_spread_price is None:
                raise ValueError("market fill requires separate spread/slippage and taker role")
            number(self.after_spread_price, "after_spread_price", positive=True)
            sha256(self.spread_policy_identity)
            sha256(self.slippage_policy_identity)
        elif (self.fee_role is not FeeRole.MAKER or self.fill_price != self.submitted_limit_price
              or any(v is not None for v in (self.after_spread_price, self.spread_policy_identity, self.slippage_policy_identity))):
            raise ValueError("passive fill remains at submitted limit without spread/slippage charges")
        if self.classification is not Provenance.FIXED_SIMULATION_ASSUMPTION or self.kind != "FILL_PROPOSAL":
            raise ValueError("simulated fill classification required")


@dataclass(frozen=True)
class TriggerProposal(ResolutionIdentity):
    binding: ProposalBinding
    parent_order_id: str
    trigger_result: Result
    child_admission: AdmissionResult
    status: str
    next_state: str
    timing_basis: str
    opening_trigger_result: Result | None = None
    kind: str = "ORDER_TRIGGER_PROPOSAL"

    def __post_init__(self):
        if (not isinstance(self.binding, ProposalBinding) or not isinstance(self.trigger_result, Result)
                or self.trigger_result.status != "VALID" or self.trigger_result.value is not True
                or not isinstance(self.child_admission, AdmissionResult)):
            raise ValueError("factual trigger result and activation-time child admission required")
        text(self.parent_order_id, "parent order ID")
        expected = ("ACTIVE" if self.child_admission.admitted is not None else
                    "TRIGGERED_CHILD_REJECTED" if self.child_admission.status == "REJECTED_RULE" else
                    "TRIGGERED_CHILD_UNAVAILABLE")
        if self.next_state != expected or self.kind != "ORDER_TRIGGER_PROPOSAL":
            raise ValueError("trigger lifecycle mismatch")
        if self.status not in ("TRIGGERED_EXACT", "TRIGGERED_WITHIN_INTERVAL", "TRIGGERED_BY_MARK_OPEN"):
            raise ValueError("trigger status mismatch")
        expected_basis = {"TRIGGERED_EXACT": "EXACT_CONTRACT_TRADE",
                          "TRIGGERED_WITHIN_INTERVAL": "CROSSED_WITHIN_MARK_MINUTE",
                          "TRIGGERED_BY_MARK_OPEN": "CONDITION_ALREADY_TRUE_AT_MARK_OPEN"}[self.status]
        if self.timing_basis != expected_basis:
            raise ValueError("trigger timing basis/status mismatch")
        if (self.status == "TRIGGERED_EXACT") != self.binding.causal.exact:
            raise ValueError("exact trigger status must match exact causal time")
        if self.timing_basis == "EXACT_CONTRACT_TRADE":
            if self.opening_trigger_result is not None:
                raise ValueError("contract-price trigger has no mark opening predicate")
        elif (not isinstance(self.opening_trigger_result, Result)
              or self.opening_trigger_result.status != "VALID"
              or self.opening_trigger_result.calculation != "trigger"
              or self.opening_trigger_result.value is not (self.timing_basis == "CONDITION_ALREADY_TRUE_AT_MARK_OPEN")):
            raise ValueError("mark opening trigger predicate must be retained exactly")
        if self.child_admission.admitted is not None and self.child_admission.admitted.activation != self.binding.causal:
            raise ValueError("child admission must bind the triggering event/interval")


@dataclass(frozen=True)
class FundingProposal(ResolutionIdentity):
    binding: ProposalBinding
    position_id: str
    scope: Scope
    quantity_at_settlement: Scalar
    funding_rate: Decimal
    exact_settlement_mark: Decimal
    cashflow: Result
    kind: str = "FUNDING_PROPOSAL"

    def __post_init__(self):
        scope(self.scope)
        text(self.position_id, "position_id")
        number(self.quantity_at_settlement, "settlement quantity", minimum=0)
        number(self.funding_rate, "funding rate")
        number(self.exact_settlement_mark, "settlement mark", positive=True)
        if (not isinstance(self.binding, ProposalBinding) or not self.binding.causal.exact
                or not isinstance(self.cashflow, Result) or self.cashflow.status != "VALID"
                or self.cashflow.calculation != "funding" or self.kind != "FUNDING_PROPOSAL"):
            raise ValueError("exact funding proposal required")


@dataclass(frozen=True)
class LiquidationRiskProposal(ResolutionIdentity):
    binding: ProposalBinding
    position_id: str
    scope: Scope
    threshold: Result
    crossing_basis: str
    kind: str = "LIQUIDATION_RISK_PROPOSAL"

    def __post_init__(self):
        scope(self.scope)
        text(self.position_id, "position_id")
        if (not isinstance(self.binding, ProposalBinding) or not isinstance(self.threshold, Result)
                or self.threshold.status != "VALID" or self.threshold.calculation != "liquidation_threshold"
                or self.threshold.bracket_identity is None or self.kind != "LIQUIDATION_RISK_PROPOSAL"
                or self.crossing_basis not in ("CROSSED_WITHIN_MARK_MINUTE", "BREACHED_BY_MARK_OPEN")):
            raise ValueError("risk proposal must retain valid Part 1 threshold and bracket identity")
        number(self.threshold.value, "risk threshold", positive=True)


@dataclass(frozen=True)
class LiquidationCloseoutProposal(ResolutionIdentity):
    binding: ProposalBinding
    position_id: str
    scope: Scope
    selected_frontier_identity: str
    risk_proposal_identity: str
    closing_side: OrderSide
    quantity: Scalar
    reference_threshold: Scalar
    simulated_closeout_price: Result
    closeout_notional: Result
    closeout_charge: Scalar
    closeout_policy_identity: str
    fee_evidence_identity: str
    normal_trading_fee: None = None
    classification: Provenance = Provenance.FIXED_SIMULATION_ASSUMPTION
    kind: str = "LIQUIDATION_CLOSEOUT_PROPOSAL"

    def __post_init__(self):
        scope(self.scope)
        text(self.position_id, "position_id")
        enum_value(self.closing_side, OrderSide)
        for value in (self.selected_frontier_identity, self.risk_proposal_identity,
                      self.closeout_policy_identity, self.fee_evidence_identity):
            sha256(value)
        number(self.quantity, "closeout quantity", positive=True)
        number(self.reference_threshold, "risk threshold", positive=True)
        number(self.closeout_charge, "separate closeout charge", minimum=0)
        if (not isinstance(self.binding, ProposalBinding)
                or not isinstance(self.simulated_closeout_price, Result)
                or self.simulated_closeout_price.status != "VALID"
                or not isinstance(self.closeout_notional, Result) or self.closeout_notional.status != "VALID"
                or self.normal_trading_fee is not None
                or self.classification is not Provenance.FIXED_SIMULATION_ASSUMPTION
                or self.kind != "LIQUIDATION_CLOSEOUT_PROPOSAL"):
            raise ValueError("closeout must remain a separate simulation approximation")


Proposal = FillProposal | TriggerProposal | FundingProposal | LiquidationRiskProposal | UnavailableProposal


@dataclass(frozen=True)
class ExecutionCandidate(ResolutionIdentity):
    proposal: Proposal
    resource_ids: tuple[str, ...]

    def __post_init__(self):
        if not isinstance(self.proposal, (FillProposal, TriggerProposal, FundingProposal,
                                          LiquidationRiskProposal, UnavailableProposal)):
            raise ValueError("typed execution candidate required")
        if not isinstance(self.resource_ids, tuple) or not self.resource_ids:
            raise ValueError("explicit affected resources required")
        for value in self.resource_ids:
            text(value, "affected resource")
        object.__setattr__(self, "resource_ids", tuple(sorted(set(self.resource_ids))))

    @property
    def causal(self):
        return self.proposal.binding.causal


@dataclass(frozen=True)
class FrontierResolution(ResolutionIdentity):
    status: str
    candidate_identities: tuple[str, ...]
    frontier: tuple[ExecutionCandidate, ...]
    proposals: tuple[Proposal, ...]
    reasons: tuple[str, ...]
    causal_policy_identity: str
    ambiguity_policy_identity: str

    def __post_init__(self):
        _hashes(self.candidate_identities)
        sha256(self.causal_policy_identity)
        sha256(self.ambiguity_policy_identity)
        if (not isinstance(self.frontier, tuple) or len(self.frontier) > MAX_FRONTIER_CANDIDATES
                or any(not isinstance(c, ExecutionCandidate) for c in self.frontier)
                or not isinstance(self.proposals, tuple) or not isinstance(self.reasons, tuple)
                or self.status not in ("NO_ACTION", "SELECTED", "AMBIGUOUS", "UNAVAILABLE")):
            raise ValueError("bounded immutable frontier result required")
        if self.status != "SELECTED" and self.proposals:
            raise ValueError("ambiguous/unavailable frontiers propose no executable actions")
        if (any(c.identity not in self.candidate_identities for c in self.frontier)
                or (self.status == "NO_ACTION") != (not self.frontier)):
            raise ValueError("frontier must bind the complete supplied candidate identity set")
        if self.status == "SELECTED" and self.proposals != tuple(c.proposal for c in self.frontier):
            raise ValueError("selected proposals must bind the selected frontier")

    @property
    def selection_identity(self):
        """Stable proof for the selected minimal frontier, excluding dominated candidates."""
        return canonical_digest({
            "algorithm": ALGORITHM_VERSION,
            "status": self.status,
            "selected_candidate_identities": tuple(sorted(c.identity for c in self.frontier)),
            "selected_proposal_identities": tuple(sorted(p.identity for p in self.proposals)),
            "causal_policy_identity": self.causal_policy_identity,
            "ambiguity_policy_identity": self.ambiguity_policy_identity,
            "reasons": tuple(sorted(self.reasons)),
        })
