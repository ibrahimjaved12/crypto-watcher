"""Pure deterministic execution proposals and bounded causal-frontier resolution.

Candidates are tentative. Only a SELECTED frontier exposes executable proposals;
#36 owns applying them exactly once and supplying subsequent immutable views.
There is no wallet, state mutation, network, clock or universal event priority.
"""

from contextlib import closing

from . import futures_execution as math
from .binance_execution_evidence import (
    ExecutionTrade, ExecutionTradeTapeEvidence, FundingEvidence, FundingSettlementEvidence,
    MarkRiskEvidence, MarkRiskMinute, iter_execution_trades,
)
from .binance_execution_snapshots import BracketSnapshot, ContractRuleSnapshot, FeeSnapshot
from .canonical_identity import canonical_digest
from .execution_evidence import ExecutionEvidenceSnapshot, SourceKind, text, timestamp
from .exact_scalar import exact_scalar, finite_or_exact
from .futures_execution_contracts import (
    ContractRules, FeeRole, OrderIntent, OrderSide, PositionSide, PriceReference,
    Provenance, RuleEvaluationContext, TriggerType,
)
from .futures_execution_resolution_contracts import (
    MAX_FRONTIER_CANDIDATES, AdmittedOrder, AdmissionResult, AmbiguityPolicy,
    CausalBounds, CausalPolicy, CloseoutPolicy, ConditionalOrderView,
    ExecutionCandidate, FillPolicy, FillProposal, FrontierResolution, FundingProposal,
    LiquidationCloseoutProposal, LiquidationRiskProposal, OrderExecutionView, OrderState,
    PositionExecutionView, ProposalBinding, TriggerProposal, UnavailableProposal,
)


def external_boundary(timestamp_ms, identity):
    """Caller-supplied submission boundary carries no cross-stream sequencing proof."""
    return CausalBounds(timestamp_ms, timestamp_ms, "external-submission", identity)


def trade_bounds(trade):
    if not isinstance(trade, ExecutionTrade):
        raise ValueError("exact execution trade required")
    return CausalBounds(trade.timestamp_ms, trade.timestamp_ms,
                        f"aggTrades:{trade.instrument_id}", trade.identity,
                        (trade.timestamp_ms, trade.aggregate_trade_id))


def mark_bounds(minute):
    if not isinstance(minute, MarkRiskMinute):
        raise ValueError("bounded one-minute mark evidence required")
    return CausalBounds(minute.candle.open_time_ms, minute.candle.close_time_ms,
                        f"markPrice:{minute.instrument_id}", minute.identity)


def definitely_precedes(a, b, policy=CausalPolicy()):
    if not isinstance(a, CausalBounds) or not isinstance(b, CausalBounds) or not isinstance(policy, CausalPolicy):
        raise ValueError("explicit causal bounds and policy required")
    return (a.latest_possible_ms < b.earliest_possible_ms
            or a.stream_id == b.stream_id and a.sequence_key is not None and b.sequence_key is not None
            and a.sequence_key < b.sequence_key)


def _component(evidence, name, value):
    if (not isinstance(evidence, ExecutionEvidenceSnapshot)
            or value.instrument_id != evidence.scope.instrument_id):
        raise ValueError("component/snapshot instrument mismatch")
    ref = next(r for r in evidence.components if r.component == name)
    if ref.evidence_identity != value.identity:
        raise ValueError(f"{name} snapshot/component identity mismatch")


def _snapshot_applicable(snapshot, earliest, latest):
    p = snapshot.source.provenance
    if (p.kind is SourceKind.HISTORICAL_SNAPSHOT and p.classification is Provenance.ACTUAL_HISTORICAL
            and not p.effective_at_ms <= earliest <= latest <= p.historical_valid_until_ms):
        raise ValueError("historical snapshot does not apply to this execution boundary")


def admit_order(order_id, position_id, intent, rules, evidence, admitted_at_ms, *,
                context=None, activation=None, conditional_parent_identity=None):
    """Freeze exact Part 1 admission inputs; never infer a mark from candles."""
    text(order_id, "order_id")
    text(position_id, "position_id")
    timestamp(admitted_at_ms)
    if not isinstance(intent, OrderIntent) or not isinstance(evidence, ExecutionEvidenceSnapshot):
        raise ValueError("explicit intent and evidence snapshot required")
    if context is not None and not isinstance(context, RuleEvaluationContext):
        raise ValueError("exact rule context required; mark candles are not admission contexts")
    if isinstance(rules, ContractRuleSnapshot):
        _component(evidence, "contract_rules", rules)
        _snapshot_applicable(rules, admitted_at_ms, admitted_at_ms)
        rule_identity, normalized = rules.identity, rules.rules
    elif isinstance(rules, ContractRules):
        rule_identity, normalized = rules.identity, rules
        ref = next(r for r in evidence.components if r.component == "contract_rules")
        if ref.evidence_identity != rule_identity:
            raise ValueError("explicit Part 1 rule identity must match the snapshot reference")
    else:
        raise ValueError("ContractRuleSnapshot or ContractRules required")
    if normalized.scope != evidence.scope:
        raise ValueError("rule/admission scope mismatch")
    if activation is None:
        activation = external_boundary(admitted_at_ms, canonical_digest(
            ("submission", order_id, position_id, intent, rule_identity, evidence.identity, context, admitted_at_ms)))
    if not isinstance(activation, CausalBounds) or activation.latest_possible_ms != admitted_at_ms:
        raise ValueError("admission timestamp must bind its activation boundary")
    if activation.sequence_key is not None and activation.stream_id != f"aggTrades:{evidence.scope.instrument_id}":
        raise ValueError("activation stream instrument mismatch")
    validation = math.validate_order(intent, normalized, context)
    inputs = canonical_digest((order_id, position_id, intent, rule_identity, evidence.identity,
                               context, activation, conditional_parent_identity))
    admitted = None
    if validation.status == "VALID":
        admitted = AdmittedOrder(order_id, position_id, normalized.scope, intent, normalized,
                                 rule_identity, validation, context, evidence, activation, conditional_parent_identity)
    return AdmissionResult(validation, admitted, inputs)


def _trade_evidence(trade, tape, evidence):
    if not isinstance(trade, ExecutionTrade) or not isinstance(tape, ExecutionTradeTapeEvidence):
        raise ValueError("exact trade and bounded tape manifest required")
    _component(evidence, "aggtrades", tape)
    key = (trade.timestamp_ms, trade.aggregate_trade_id)
    if (trade.instrument_id != tape.instrument_id or trade.package not in tape.packages
            or tape.row_count == 0 or not tape.first_event_key <= key <= tape.last_event_key):
        raise ValueError("trade/package/key must bind the exact supplied tape")


def _mark_evidence(minute, marks, evidence):
    if not isinstance(minute, MarkRiskMinute) or not isinstance(marks, MarkRiskEvidence):
        raise ValueError("mark minute and existing risk evidence required")
    _component(evidence, "mark_price", marks)
    if minute not in marks.minutes or minute.upstream_evidence_sha256 != marks.upstream_evidence_sha256:
        raise ValueError("mark minute must bind the supplied mark evidence")


def _position_resource(scope, position_id):
    return f"position:{scope.instrument_id}:{position_id}"


def _order_resources(admitted):
    return (_position_resource(admitted.scope, admitted.position_id),
            f"order:{admitted.scope.instrument_id}:{admitted.order_id}")


def _binding(input_view, evidence, component, causal, *policies):
    return ProposalBinding(input_view.identity, evidence.identity, component.identity,
                           causal, tuple(p.identity for p in policies))


def _unavailable(binding, resources, status, *, calculation=None, related=()):
    return ExecutionCandidate(UnavailableProposal(binding, status, (status,), calculation, related), resources)


def _activation_relation(activation, event):
    if definitely_precedes(activation, event):
        return "AFTER"
    if (activation.stream_id == event.stream_id and activation.sequence_key is not None
            and event.sequence_key is not None):
        return "NOT_AFTER"  # The trigger print and any earlier print are excluded.
    if event.latest_possible_ms < activation.earliest_possible_ms:
        return "NOT_AFTER"
    if not activation.exact:
        return "NOT_AFTER"  # Interval activation guarantees execution only after its end.
    return "UNRESOLVED"


def fill_candidate(view, trade, tape, fees, policy, evidence):
    """Propose one fill from one exact print; never update remaining state here."""
    if not isinstance(view, OrderExecutionView) or not isinstance(policy, FillPolicy) or not isinstance(fees, FeeSnapshot):
        raise ValueError("admitted order view, fill policy and fee snapshot required")
    _trade_evidence(trade, tape, evidence)
    _component(evidence, "fees", fees)
    admitted = view.admitted
    if evidence.scope != admitted.scope or fees.scope != admitted.scope:
        raise ValueError("fill evidence/order scope mismatch")
    if view.state not in (OrderState.ACTIVE, OrderState.PARTIAL):
        return None
    intent = admitted.intent
    if not intent.market:
        opposite = OrderSide.SELL if intent.side is OrderSide.BUY else OrderSide.BUY
        through = trade.price < intent.price if intent.side is OrderSide.BUY else trade.price > intent.price
        if trade.aggressor_side is not opposite or not through:
            return None
    causal = trade_bounds(trade)
    relation = _activation_relation(admitted.activation, causal)
    if relation == "NOT_AFTER":
        return None
    if view.last_fill_event is not None and not definitely_precedes(view.last_fill_event, causal):
        return None
    binding = _binding(view, evidence, tape, causal, policy, fees)
    resources = _order_resources(admitted)
    if not intent.market:
        resources += (f"print-volume:{trade.identity}",)
    if relation == "UNRESOLVED":
        return _unavailable(binding, resources, "AMBIGUOUS_ACTIVATION_ORDER",
                            related=(admitted.activation.source_event_identity, trade.identity))
    _snapshot_applicable(fees, trade.timestamp_ms, trade.timestamp_ms)
    if policy.evidence.classification is Provenance.UNAVAILABLE:
        return _unavailable(binding, resources, "UNAVAILABLE_FILL_POLICY")
    quantity = view.remaining_quantity if intent.market else min(view.remaining_quantity, trade.quantity)
    after_spread = spread_identity = slippage_identity = None
    if intent.market:
        spread = math.adverse_price(trade.price, intent.side, policy.spread)
        if spread.status != "VALID":
            return _unavailable(binding, resources, spread.status, calculation=spread)
        slippage = math.adverse_price(spread.value, intent.side, policy.slippage)
        if slippage.status != "VALID":
            return _unavailable(binding, resources, slippage.status, calculation=slippage)
        after_spread, price = spread.value, slippage.value
        spread_identity, slippage_identity = policy.spread.identity, policy.slippage.identity
        role = FeeRole.TAKER
    else:
        price, role = intent.price, FeeRole.MAKER
    fee = math.fee(quantity, price, role, fees.policy)
    if fee.status != "VALID":
        return _unavailable(binding, resources, fee.status, calculation=fee)
    remaining = finite_or_exact(exact_scalar(view.remaining_quantity) - exact_scalar(quantity))
    status = "FILLED" if remaining == 0 else "PARTIAL"
    proposal = FillProposal(binding, admitted.order_id, admitted.identity, admitted.scope, intent.side,
                            intent.quantity, view.remaining_quantity, quantity, remaining, intent.price,
                            trade.price, after_spread, price, role, fee, spread_identity,
                            slippage_identity, policy.identity, status, OrderState(status))
    return ExecutionCandidate(proposal, resources)


def next_fill_candidate(view, trades, tape, fees, policy, evidence):
    """Consume only until the first eligible/unresolved candidate; no population buffer.

    The source must be a canonical stream bound to the manifest. For historical
    roots prefer next_tape_fill_candidate, which verifies that binding and closes
    its temporary iterator. This helper retains only one print at a time.
    """
    if not isinstance(view, OrderExecutionView):
        raise ValueError("admitted order execution view required")
    if view.state not in (OrderState.ACTIVE, OrderState.PARTIAL):
        return None
    previous = None
    for trade in trades:
        if not isinstance(trade, ExecutionTrade):
            raise ValueError("canonical execution trade stream required")
        key = (trade.timestamp_ms, trade.aggregate_trade_id)
        if previous is not None and key <= previous:
            raise ValueError("execution trade iterator must be canonically ordered and deduplicated")
        previous = key
        candidate = fill_candidate(view, trade, tape, fees, policy, evidence)
        if candidate is not None:
            return candidate
    return None


def next_tape_fill_candidate(archive_root, view, tape, fees, policy, evidence):
    """Bounded historical production path; temporary evidence index closes on early return."""
    if not isinstance(view, OrderExecutionView):
        raise ValueError("admitted order execution view required")
    if view.state not in (OrderState.ACTIVE, OrderState.PARTIAL):
        return None
    with closing(iter_execution_trades(archive_root, tape)) as trades:
        return next_fill_candidate(view, trades, tape, fees, policy, evidence)


def conditional_candidate(view, event, rules, evidence, *, context=None, tape=None, marks=None):
    """Trigger facts and child admission occur together at the activation boundary."""
    if not isinstance(view, ConditionalOrderView):
        raise ValueError("immutable conditional view required")
    order = view.order
    if evidence.scope != order.scope:
        raise ValueError("conditional evidence scope mismatch")
    if view.state != "WAITING_TRIGGER":
        return None
    if order.trigger.reference is PriceReference.CONTRACT_PRICE:
        _trade_evidence(event, tape, evidence)
        causal, component = trade_bounds(event), tape
        predicate = math.trigger_predicate(order.trigger, contract_price=event.price)
        opening_predicate = None
        timing_basis = "EXACT_CONTRACT_TRADE"
    else:
        _mark_evidence(event, marks, evidence)
        component = marks
        greater = ((order.trigger.kind is TriggerType.STOP and order.trigger.side is OrderSide.BUY)
                   or (order.trigger.kind is TriggerType.TAKE_PROFIT and order.trigger.side is OrderSide.SELL))
        extreme = event.candle.high if greater else event.candle.low
        opening_predicate = math.trigger_predicate(order.trigger, mark_price=event.candle.open)
        extreme_predicate = math.trigger_predicate(order.trigger, mark_price=extreme)
        if opening_predicate.status != "VALID":
            causal = mark_bounds(event)
            predicate = opening_predicate
            timing_basis = "CROSSED_WITHIN_MARK_MINUTE"
        elif extreme_predicate.status != "VALID":
            causal = mark_bounds(event)
            predicate = extreme_predicate
            timing_basis = "CROSSED_WITHIN_MARK_MINUTE"
        elif not extreme_predicate.value:
            return None
        elif opening_predicate.value:
            if order.creation.latest_possible_ms < event.candle.open_time_ms:
                causal = CausalBounds(order.creation.earliest_possible_ms,
                                      event.candle.open_time_ms,
                                      f"markPrice:{event.instrument_id}", event.identity)
                predicate = opening_predicate
                timing_basis = "CONDITION_ALREADY_TRUE_AT_MARK_OPEN"
            else:
                if order.creation.earliest_possible_ms > event.candle.close_time_ms:
                    return None
                earliest = order.creation.earliest_possible_ms
                causal = CausalBounds(earliest, event.candle.close_time_ms,
                                      f"markPrice:{event.instrument_id}", event.identity)
                predicate = opening_predicate
                binding = _binding(view, evidence, component, causal, CausalPolicy(), AmbiguityPolicy())
                resources = (_position_resource(order.scope, order.position_id),
                             f"order:{order.scope.instrument_id}:{order.order_id}")
                return _unavailable(binding, resources, "AMBIGUOUS_TRIGGER_ACTIVATION_ORDER",
                                    related=(order.creation.source_event_identity, causal.source_event_identity))
        else:
            causal = mark_bounds(event)
            predicate = extreme_predicate
            timing_basis = "CROSSED_WITHIN_MARK_MINUTE"
    binding = _binding(view, evidence, component, causal, CausalPolicy(), AmbiguityPolicy())
    resources = (_position_resource(order.scope, order.position_id),
                 f"order:{order.scope.instrument_id}:{order.order_id}")
    relation = ("AFTER" if timing_basis == "CONDITION_ALREADY_TRUE_AT_MARK_OPEN"
                else _activation_relation(order.creation, causal))
    if relation == "NOT_AFTER":
        return None
    if predicate.status != "VALID":
        return _unavailable(binding, resources, predicate.status, calculation=predicate)
    if not predicate.value:
        return None
    # A mark minute overlapping creation cannot establish that its crossing was
    # after submission. Unlike already-triggered child activation, keep it unresolved.
    if relation == "UNRESOLVED" or (timing_basis != "CONDITION_ALREADY_TRUE_AT_MARK_OPEN"
                                     and not causal.exact and not definitely_precedes(order.creation, causal)):
        return _unavailable(binding, resources, "AMBIGUOUS_TRIGGER_ACTIVATION_ORDER",
                            related=(order.creation.source_event_identity, causal.source_event_identity))
    admission = admit_order(order.child_order_id, order.position_id, order.child_intent, rules,
                            evidence, causal.latest_possible_ms, context=context, activation=causal,
                            conditional_parent_identity=order.identity)
    status = ("TRIGGERED_EXACT" if timing_basis == "EXACT_CONTRACT_TRADE" else
              "TRIGGERED_BY_MARK_OPEN" if timing_basis == "CONDITION_ALREADY_TRUE_AT_MARK_OPEN" else
              "TRIGGERED_WITHIN_INTERVAL")
    next_state = ("ACTIVE" if admission.admitted is not None else
                  "TRIGGERED_CHILD_REJECTED" if admission.status == "REJECTED_RULE" else
                  "TRIGGERED_CHILD_UNAVAILABLE")
    proposal = TriggerProposal(binding, order.order_id, predicate, admission, status,
                               next_state, timing_basis, opening_predicate)
    return ExecutionCandidate(proposal, resources)


def funding_candidate(position, event, funding_evidence, evidence):
    if not isinstance(position, PositionExecutionView) or not isinstance(event, FundingSettlementEvidence) or not isinstance(funding_evidence, FundingEvidence):
        raise ValueError("position view and exact funding evidence contracts required")
    _component(evidence, "funding", funding_evidence)
    if position.scope != evidence.scope or event not in funding_evidence.events:
        raise ValueError("funding position/event/evidence mismatch")
    causal = CausalBounds(event.funding_timestamp_ms, event.funding_timestamp_ms,
                          f"funding:{event.instrument_id}", event.identity)
    binding = _binding(position, evidence, funding_evidence, causal, CausalPolicy(), AmbiguityPolicy())
    resources = (_position_resource(position.scope, position.position_id),)
    if not position.covers(event.funding_timestamp_ms, event.funding_timestamp_ms):
        return _unavailable(binding, resources, "UNAVAILABLE_POSITION_AT_SETTLEMENT")
    amount = math.funding(position.side, position.quantity, event.funding_rate, event.settlement_mark)
    if amount.status != "VALID":
        return _unavailable(binding, resources, amount.status, calculation=amount)
    return ExecutionCandidate(FundingProposal(binding, position.position_id, position.scope, position.quantity,
                                               event.funding_rate, event.settlement_mark, amount), resources)


def liquidation_candidate(position, minute, marks, brackets, evidence):
    if not isinstance(position, PositionExecutionView) or not isinstance(brackets, BracketSnapshot):
        raise ValueError("position view and normalized bracket snapshot required")
    _mark_evidence(minute, marks, evidence)
    _component(evidence, "brackets", brackets)
    if position.scope != evidence.scope or brackets.scope != position.scope:
        raise ValueError("liquidation scope mismatch")
    resources = (_position_resource(position.scope, position.position_id),)
    minute_causal = mark_bounds(minute)
    binding = _binding(position, evidence, marks, minute_causal, brackets, CausalPolicy(), AmbiguityPolicy())
    threshold = math.liquidation_threshold(position.side, position.quantity, position.average_entry,
                                           position.isolated_wallet_collateral, brackets.table)
    if threshold.status in ("NO_POSITION", "NO_POSITIVE_THRESHOLD"):
        return None
    if threshold.status != "VALID":
        binding = _binding(position, evidence, marks, minute_causal, brackets, CausalPolicy(), AmbiguityPolicy())
        return _unavailable(binding, resources, threshold.status, calculation=threshold)
    open_breached = (minute.candle.open <= threshold.value if position.side is PositionSide.LONG
                     else minute.candle.open >= threshold.value)
    reached = (minute.candle.low <= threshold.value if position.side is PositionSide.LONG
               else minute.candle.high >= threshold.value)
    if not reached:
        return None
    if open_breached:
        causal = CausalBounds(position.effective_from_ms, minute.candle.open_time_ms,
                              f"markPrice:{minute.instrument_id}", minute.identity)
        crossing_basis = "BREACHED_BY_MARK_OPEN"
    else:
        causal = minute_causal
        crossing_basis = "CROSSED_WITHIN_MARK_MINUTE"
    if not position.covers(causal.earliest_possible_ms, causal.latest_possible_ms):
        binding = _binding(position, evidence, marks, causal, brackets, CausalPolicy(), AmbiguityPolicy())
        status = ("UNAVAILABLE_POSITION_AT_BREACHED_MARK_OPEN" if open_breached
                  else "UNAVAILABLE_POSITION_DURING_MARK_INTERVAL")
        return _unavailable(binding, resources, status)
    _snapshot_applicable(brackets, causal.earliest_possible_ms, causal.latest_possible_ms)
    binding = _binding(position, evidence, marks, causal, brackets, CausalPolicy(), AmbiguityPolicy())
    return ExecutionCandidate(LiquidationRiskProposal(binding, position.position_id, position.scope,
                                                       threshold, crossing_basis), resources)


def resolve_causal_frontier(candidates, *, causal_policy=CausalPolicy(), ambiguity_policy=AmbiguityPolicy()):
    """Resolve a small current candidate window, never a global historical event list.

    The caller supplies all potentially competing events for the current window;
    absence of a stream cannot prove absence of a competing event. Related order
    and funding/risk candidates must use the same stable position_id. Sorted IDs
    make presentation/identity deterministic; they never decide execution order.
    """
    if not isinstance(causal_policy, CausalPolicy) or not isinstance(ambiguity_policy, AmbiguityPolicy):
        raise ValueError("explicit versioned causal and ambiguity policies required")
    bounded = {}
    for candidate in candidates:
        if not isinstance(candidate, ExecutionCandidate):
            raise ValueError("typed current-window execution candidates required")
        bounded[candidate.identity] = candidate
        if len(bounded) > MAX_FRONTIER_CANDIDATES:
            raise ValueError("current causal window exceeds bounded frontier capacity")
    values = tuple(bounded[key] for key in sorted(bounded))
    frontier = tuple(c for c in values if not any(
        other.identity != c.identity and definitely_precedes(other.causal, c.causal, causal_policy)
        for other in values))
    status, reasons, proposals = "NO_ACTION", (), ()
    if frontier:
        competing = any(_materially_compete(a, b)
                        for i, a in enumerate(frontier) for b in frontier[i + 1:])
        intrinsic = any(isinstance(c.proposal, UnavailableProposal)
                        and c.proposal.status.startswith("AMBIGUOUS") for c in frontier)
        unavailable = any(isinstance(c.proposal, UnavailableProposal) for c in frontier)
        if competing or intrinsic:
            status, reasons = "AMBIGUOUS", ("CAUSALLY_INCOMPARABLE_MATERIAL_EVENTS",)
        elif unavailable:
            status, reasons = "UNAVAILABLE", ("UNAVAILABLE_FRONTIER_INPUT",)
        else:
            status, proposals = "SELECTED", tuple(c.proposal for c in frontier)
    return FrontierResolution(status, tuple(c.identity for c in values), frontier, proposals, reasons,
                              causal_policy.identity, ambiguity_policy.identity)


def _materially_compete(a, b):
    if not set(a.resource_ids) & set(b.resource_ids):
        return False
    # Two exact funding charges against the same confirmed position view commute.
    # Position-changing fills, lifecycle changes and risk/closeout never gain an
    # invented priority from their type. Unknown cases remain competing.
    return not (isinstance(a.proposal, FundingProposal) and isinstance(b.proposal, FundingProposal)
                and a.proposal.binding.input_identity == b.proposal.binding.input_identity)


def activated_child_view(resolution, conditional_view):
    """Expose a child view only from an unambiguously selected trigger proposal.

    The returned view is still a proposal of #36's next state, never committed state.
    """
    if not isinstance(resolution, FrontierResolution) or resolution.status != "SELECTED" or not isinstance(conditional_view, ConditionalOrderView):
        raise ValueError("selected trigger frontier and matching conditional view required")
    triggers = tuple(p for p in resolution.proposals if isinstance(p, TriggerProposal)
                     and p.binding.input_identity == conditional_view.identity)
    if len(triggers) != 1 or triggers[0].child_admission.admitted is None:
        raise ValueError("selected trigger must admit this child")
    admitted = triggers[0].child_admission.admitted
    return OrderExecutionView(admitted, admitted.intent.quantity)


def liquidation_closeout(resolution, position, policy):
    """Only an unambiguously selected risk can become a simulated closeout proposal."""
    if not isinstance(resolution, FrontierResolution) or not isinstance(position, PositionExecutionView) or not isinstance(policy, CloseoutPolicy):
        raise ValueError("selected frontier, position view and closeout policy required")
    if resolution.status != "SELECTED":
        raise ValueError("liquidation closeout requires an unambiguously selected risk event")
    risks = tuple(p for p in resolution.proposals if isinstance(p, LiquidationRiskProposal)
                  and p.position_id == position.position_id and p.scope == position.scope)
    if len(risks) != 1 or risks[0].binding.input_identity != position.identity:
        raise ValueError("selected risk must bind this exact position view")
    risk = risks[0]
    binding = ProposalBinding(position.identity, risk.binding.evidence_identity,
                              risk.binding.source_evidence_identity, risk.binding.causal,
                              risk.binding.policy_identities + (policy.identity, resolution.selection_identity))
    if any(e.classification is Provenance.UNAVAILABLE for e in
           (policy.evidence, policy.adverse_bps.evidence, policy.fee_evidence)):
        return UnavailableProposal(binding, "UNAVAILABLE_CLOSEOUT_POLICY", ("UNAVAILABLE_CLOSEOUT_POLICY",))
    side = OrderSide.SELL if position.side is PositionSide.LONG else OrderSide.BUY
    price = math.adverse_price(risk.threshold.value, side, policy.adverse_bps)
    if price.status != "VALID":
        return UnavailableProposal(binding, price.status, price.reasons, price)
    notional = math.notional(position.quantity, price.value)
    charge = finite_or_exact(exact_scalar(notional.value) * exact_scalar(policy.fee_rate))
    return LiquidationCloseoutProposal(binding, position.position_id, position.scope, resolution.selection_identity,
                                       risk.identity, side, position.quantity, risk.threshold.value, price,
                                       notional, charge, policy.identity, policy.fee_evidence.identity)
