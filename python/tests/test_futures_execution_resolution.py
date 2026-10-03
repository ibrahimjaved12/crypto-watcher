"""Focused frozen #37 Part 3 fixtures; synthetic tuples are test fixtures only."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal, localcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from market_analysis import futures_execution as math
from market_analysis import futures_execution_resolution as resolution
from market_analysis import futures_execution_resolution_contracts as resolution_contracts
from market_analysis.binance_execution_evidence import (
    FundingEvidence, iter_execution_trades, join_settlement_mark,
    load_execution_mark_risk_evidence, load_execution_trade_tape,
)
from market_analysis.binance_execution_snapshots import normalize_bracket_snapshot
from market_analysis.canonical_identity import canonical_digest
from market_analysis.execution_evidence import execution_evidence_snapshot
from market_analysis.execution_evidence import ExecutionEvidenceSnapshot, SourceKind
from market_analysis.exact_scalar import exact_scalar
from market_analysis.futures_execution_contracts import (
    ALGORITHM_VERSION as MATH_VERSION, FeeRole, FixedBpsPolicy, OrderIntent,
    OrderSide, PositionSide, PriceReference, Provenance, RuleEvaluationContext, Trigger, TriggerType,
)
from market_analysis.futures_execution_resolution_contracts import (
    ALGORITHM_VERSION, MAX_FRONTIER_CANDIDATES, AdmittedOrder, AmbiguityPolicy, CausalBounds,
    CloseoutPolicy, ConditionalOrder, ConditionalOrderView, ExecutionCandidate, FillPolicy,
    FillProposal, FundingProposal, LiquidationCloseoutProposal, LiquidationRiskProposal,
    OrderExecutionView, OrderState, PositionExecutionView, SIMULATION_EVIDENCE, UnavailableProposal,
)
from market_analysis.binance_historical_archive import daily_aggtrades_relative_path
from market_analysis.historical_mark_price_evidence import daily_mark_price_relative_path

from test_execution_evidence import (
    DAY, HASH, INSTRUMENT, SCOPE, SYMBOL, TIME, agg, archive, bracket_payload, brackets,
    encoded, exact_mark, fees, funding, mark_row, rules, current,
)


D = Decimal
BUY, SELL = OrderSide.BUY, OrderSide.SELL
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT


def policy(spread="0", slippage="0", **options):
    return FillPolicy(FixedBpsPolicy(D(spread), SIMULATION_EVIDENCE),
                      FixedBpsPolicy(D(slippage), SIMULATION_EVIDENCE), **options)


def print_row(ident=10, timestamp=TIME, price="100", quantity="0.3", aggressor=SELL):
    row = agg(ident, timestamp, price, maker="true" if aggressor is SELL else "false")
    row[2] = quantity
    return row


class Fixture:
    def __init__(self, root, trade_rows, mark_prices):
        self.root = root
        archive(root, daily_aggtrades_relative_path(SYMBOL, DAY), trade_rows)
        row = mark_row(TIME)
        row[1:5] = mark_prices
        archive(root, daily_mark_price_relative_path(SYMBOL, DAY), [row])
        self.tape = load_execution_trade_tape(root, SYMBOL, [DAY])
        self.trades = tuple(iter_execution_trades(root, self.tape))
        self.marks = load_execution_mark_risk_evidence(root, SYMBOL, TIME + 60000, TIME + 60000)
        self.minute = self.marks.minutes[0]
        self.rules, self.fees, self.brackets = rules(), fees(), brackets()
        event = funding()
        self.event = join_settlement_mark(event, exact_mark(event))
        self.funding = FundingEvidence(SYMBOL, INSTRUMENT, (self.event,), HASH)
        self.evidence = execution_evidence_snapshot(SCOPE, contract_rules=self.rules, fees=self.fees,
            brackets=self.brackets, aggtrades=self.tape, mark_price=self.marks, funding=self.funding)
        self.context = RuleEvaluationContext(D(100), self.rules.rules.evidence)

    def view(self, side=BUY, market=True, quantity=None, price="100", activation=None, admitted_at=None,
             order_id="order-1", context=True):
        quantity = D(quantity or ("2" if market else "1.1"))
        intent = OrderIntent(side, quantity, None if market else D(price), market=market)
        at = admitted_at if admitted_at is not None else (activation.latest_possible_ms if activation is not None else TIME - 1)
        result = resolution.admit_order(order_id, "position-1", intent, self.rules, self.evidence,
                                        at, context=self.context if context else None, activation=activation)
        return OrderExecutionView(result.admitted, quantity)

    def fill(self, view, trade=None, fill_policy=None):
        return resolution.fill_candidate(view, trade or self.trades[0], self.tape, self.fees,
                                         fill_policy or policy(), self.evidence)

    def position(self, side=LONG, quantity="2", **options):
        return PositionExecutionView("position-1", SCOPE, side, D(quantity), D(100), D(20),
                                      TIME - 1000, TIME + 120000, **options)

    def conditional(self, side=BUY, kind=TriggerType.STOP, reference=PriceReference.CONTRACT_PRICE,
                    trigger="100", market=True, price="100", **options):
        child = OrderIntent(side, D("2" if market else "1.1"), None if market else D(price), market=market)
        order = ConditionalOrder("parent-1", "child-1", "position-1", SCOPE,
                                  Trigger(side, kind, reference, D(trigger), **options), child,
                                  resolution.external_boundary(TIME - 1, HASH), self.evidence.identity)
        return ConditionalOrderView(order)

    def trigger(self, view, event=None, context=True):
        event = event or (self.trades[0] if view.order.trigger.reference is PriceReference.CONTRACT_PRICE else self.minute)
        return resolution.conditional_candidate(view, event, self.rules, self.evidence,
            context=self.context if context else None, tape=self.tape, marks=self.marks)

    def risk(self, position=None):
        return resolution.liquidation_candidate(position or self.position(), self.minute,
                                                self.marks, self.brackets, self.evidence)


@contextmanager
def fixture(trades=None, mark_prices=("100", "130", "80", "100")):
    with tempfile.TemporaryDirectory() as folder:
        yield Fixture(Path(folder), trades or [print_row()], mark_prices)


class AdmissionTests(unittest.TestCase):
    def test_valid_admission_freezes_rules_context_and_evidence(self):
        with fixture() as f:
            view = f.view()
            a = view.admitted
            self.assertEqual(a.validation.status, "VALID")
            self.assertEqual(a.validation_identity, math.validate_order(a.intent, f.rules.rules, f.context).identity)
            self.assertEqual(a.contract_rule_identity, f.rules.rules.identity)
            self.assertEqual(a.rule_input_identity, f.rules.identity)
            self.assertEqual(a.evidence.identity, f.evidence.identity)
            self.assertEqual(a.admission_timestamp_ms, TIME - 1)
            self.assertEqual(a.context, f.context)
            with self.assertRaises(FrozenInstanceError):
                a.intent = replace(a.intent, quantity=D(1))
            self.assertEqual(MATH_VERSION, "binance-usdm-execution-math-v4:exact-rational")
            self.assertEqual(ALGORITHM_VERSION, "binance-usdm-execution-resolution-v3")
            self.assertNotEqual(ALGORITHM_VERSION, MATH_VERSION)

    def test_invalid_unavailable_and_candle_context_never_admit_or_adjust(self):
        with fixture() as f:
            invalid = OrderIntent(BUY, D(".3"), None, market=True)
            result = resolution.admit_order("bad", "position-1", invalid, f.rules, f.evidence, TIME - 1, context=f.context)
            self.assertEqual(result.status, "REJECTED_RULE")
            self.assertIsNone(result.admitted)
            self.assertEqual(invalid.quantity, D(".3"))
            with self.assertRaises(ValueError):
                OrderExecutionView(result.admitted, D(".3"))
            market = OrderIntent(BUY, D(2), None, market=True)
            missing = resolution.admit_order("missing", "position-1", market, f.rules, f.evidence, TIME - 1)
            self.assertEqual(missing.status, "UNAVAILABLE_RULE")
            self.assertIsNone(missing.admitted)
            with self.assertRaises(ValueError):
                resolution.admit_order("bad-context", "position-1", market, f.rules, f.evidence,
                                       TIME - 1, context=f.minute)
            with self.assertRaises(ValueError):
                replace(f.view().admitted, validation=result.validation)

    def test_raw_part1_rules_are_supported_and_fabricated_valid_proof_is_rejected(self):
        with fixture() as f:
            evidence = replace(f.evidence, components=tuple(
                replace(ref, evidence_identity=f.rules.rules.identity) if ref.component == "contract_rules" else ref
                for ref in f.evidence.components))
            intent = OrderIntent(BUY, D(2), None, market=True)
            admitted = resolution.admit_order("raw-rules", "position-1", intent, f.rules.rules,
                                             evidence, TIME - 1, context=f.context).admitted
            self.assertEqual(admitted.contract_rule_identity, f.rules.rules.identity)
            invalid = replace(intent, quantity=D(".3"))
            invalid_validation = math.validate_order(invalid, f.rules.rules, f.context)
            forged = replace(invalid_validation, status="VALID", reasons=())
            with self.assertRaisesRegex(ValueError, "actual Part 1 validation"):
                AdmittedOrder("forged", "position-1", SCOPE, invalid, f.rules.rules,
                    f.rules.rules.identity, forged, f.context, evidence, admitted.activation)

    def test_historical_rules_must_cover_actual_activation_not_parent_creation(self):
        with fixture() as f:
            provenance = current(kind=SourceKind.HISTORICAL_SNAPSHOT, classification=Provenance.ACTUAL_HISTORICAL,
                                 applicable_at_ms=TIME - 1, effective_at_ms=TIME - 10,
                                 historical_valid_until_ms=TIME - 1)
            historical = rules(provenance=provenance)
            evidence = execution_evidence_snapshot(SCOPE, contract_rules=historical, aggtrades=f.tape, fees=f.fees)
            child = OrderIntent(BUY, D(2), None, market=True)
            before = resolution.admit_order("before", "position-1", child, historical, evidence,
                                            TIME - 1, context=f.context)
            self.assertIsNotNone(before.admitted)
            with self.assertRaisesRegex(ValueError, "historical snapshot does not apply"):
                resolution.conditional_candidate(f.conditional(), f.trades[0], historical,
                    evidence, context=f.context, tape=f.tape)


class MarketFillTests(unittest.TestCase):
    def test_buy_sell_full_fill_separate_adverse_adjustments_and_taker_fee(self):
        with fixture() as f:
            p = policy("10", "20")
            for side in (BUY, SELL):
                with self.subTest(side=side), localcontext() as context:
                    context.prec = 1
                    view = f.view(side)
                    proposal = f.fill(view, fill_policy=p).proposal
                    spread = math.adverse_price(D(100), side, p.spread)
                    slip = math.adverse_price(spread.value, side, p.slippage)
                    self.assertEqual(proposal.anchor_contract_price, D(100))
                    self.assertEqual(proposal.after_spread_price, spread.value)
                    self.assertEqual(proposal.fill_price, slip.value)
                    self.assertEqual(proposal.fill_quantity, D(2))
                    self.assertEqual(proposal.remaining_after, D(0))
                    self.assertEqual(proposal.status, "FILLED")
                    self.assertEqual(proposal.fee_role, FeeRole.TAKER)
                    self.assertEqual(proposal.normal_trading_fee, math.fee(D(2), slip.value, FeeRole.TAKER, f.fees.policy))
                    self.assertEqual(proposal.spread_policy_identity, p.spread.identity)
                    self.assertEqual(proposal.slippage_policy_identity, p.slippage.identity)
                    self.assertEqual(proposal.binding.causal.sequence_key, (TIME, 10))
                    self.assertEqual(proposal.binding.causal.source_event_identity, f.trades[0].identity)
                    self.assertEqual(proposal.classification, Provenance.FIXED_SIMULATION_ASSUMPTION)
                    if side is BUY:
                        self.assertGreater(proposal.fill_price, D(100))
                    else:
                        self.assertLess(proposal.fill_price, D(100))

    def test_next_provably_later_trade_and_zero_bps(self):
        with fixture([print_row(9, TIME - 2), print_row(10, TIME), print_row(11, TIME + 1)]) as f:
            view = f.view()
            candidate = resolution.next_fill_candidate(view, iter(f.trades), f.tape, f.fees, policy(), f.evidence)
            self.assertEqual(candidate.causal.sequence_key, (TIME, 10))
            self.assertEqual(candidate.proposal.fill_price, D(100))
            self.assertEqual(candidate.proposal.after_spread_price, D(100))
            self.assertEqual(candidate.proposal.fill_quantity, view.remaining_quantity)

    def test_same_time_external_submission_is_unresolved_not_silently_skipped(self):
        with fixture([print_row(10), print_row(11, TIME + 1)]) as f:
            view = f.view(admitted_at=TIME)
            first = resolution.next_fill_candidate(view, iter(f.trades), f.tape, f.fees, policy(), f.evidence)
            self.assertIsInstance(first.proposal, UnavailableProposal)
            self.assertEqual(first.proposal.status, "AMBIGUOUS_ACTIVATION_ORDER")
            frontier = resolution.resolve_causal_frontier((first,))
            self.assertEqual(frontier.status, "AMBIGUOUS")
            self.assertEqual(frontier.proposals, ())
            self.assertIn(view.admitted.activation.source_event_identity, first.proposal.related_event_identities)
            self.assertIsInstance(f.fill(view, f.trades[1]).proposal, FillProposal)

    def test_identity_binds_policy_event_and_order_view(self):
        with fixture([print_row(10), print_row(11)]) as f:
            view = f.view()
            base = f.fill(view).proposal
            self.assertEqual(base.proposal_id, f.fill(view).proposal.proposal_id)
            self.assertNotEqual(base.proposal_id, f.fill(view, f.trades[1]).proposal.proposal_id)
            self.assertNotEqual(base.proposal_id, f.fill(view, fill_policy=policy("1")).proposal.proposal_id)
            self.assertNotEqual(base.proposal_id, f.fill(view, fill_policy=policy(slippage="1")).proposal.proposal_id)
            changed = replace(view, caller_state_identity="b" * 64)
            self.assertNotEqual(base.proposal_id, f.fill(changed).proposal.proposal_id)
            changed_evidence = replace(SIMULATION_EVIDENCE, source="another-spread-assumption")
            changed_policy = replace(policy(), spread=FixedBpsPolicy(D(0), changed_evidence))
            self.assertNotEqual(base.proposal_id, f.fill(view, fill_policy=changed_policy).proposal.proposal_id)
            with patch.object(resolution_contracts, "MAX_FRONTIER_CANDIDATES", 17):
                self.assertEqual(base.proposal_id, f.fill(view).proposal.proposal_id)
            with self.assertRaises(FrozenInstanceError):
                base.fill_quantity = D(9)

    def test_unavailable_fee_spread_and_policy_do_not_propose_fills(self):
        with fixture() as f:
            unavailable = replace(SIMULATION_EVIDENCE, classification=Provenance.UNAVAILABLE)
            for p in (replace(policy(), evidence=unavailable),
                      replace(policy(), spread=FixedBpsPolicy(D(0), unavailable)),
                      replace(policy(), slippage=FixedBpsPolicy(D(0), unavailable))):
                candidate = f.fill(f.view(), fill_policy=p)
                self.assertIsInstance(candidate.proposal, UnavailableProposal)
                self.assertEqual(resolution.resolve_causal_frontier((candidate,)).status, "UNAVAILABLE")
            unknown_fees = fees(provenance=current(classification=Provenance.UNAVAILABLE))
            evidence = execution_evidence_snapshot(SCOPE, contract_rules=f.rules, fees=unknown_fees, aggtrades=f.tape)
            view = f.view()
            result = resolution.fill_candidate(view, f.trades[0], f.tape, unknown_fees, policy(), evidence)
            self.assertIsInstance(result.proposal, UnavailableProposal)


class PassiveFillTests(unittest.TestCase):
    def test_strict_through_opposite_aggressor_for_both_sides(self):
        cases = ((BUY, SELL, "99", True), (BUY, SELL, "100", False), (BUY, BUY, "99", False),
                 (SELL, BUY, "101", True), (SELL, BUY, "100", False), (SELL, SELL, "101", False))
        for side, aggressor, price, eligible in cases:
            with self.subTest(side=side, price=price, aggressor=aggressor), fixture(
                    [print_row(price=price, aggressor=aggressor)]) as f:
                candidate = f.fill(f.view(side, market=False))
                if not eligible:
                    self.assertIsNone(candidate)
                    continue
                fill = candidate.proposal
                self.assertEqual(fill.fill_quantity, D(".3"))
                self.assertEqual(fill.remaining_after, D(".8"))
                self.assertEqual(fill.status, "PARTIAL")
                self.assertEqual(fill.fill_price, D(100))
                self.assertEqual(fill.anchor_contract_price, D(price))
                self.assertEqual(fill.fee_role, FeeRole.MAKER)
                self.assertEqual(fill.normal_trading_fee, math.fee(D(".3"), D(100), FeeRole.MAKER, f.fees.policy))
                self.assertIsNone(fill.after_spread_price)
                self.assertIsNone(fill.spread_policy_identity)
                self.assertIsNone(fill.slippage_policy_identity)

    def test_caller_supplied_remainder_cursor_continues_without_dynamic_readmission(self):
        with fixture([print_row(10, price="99"), print_row(11, price="98", quantity="2")]) as f:
            view = f.view(market=False)
            first = f.fill(view).proposal
            partial = OrderExecutionView(view.admitted, first.remaining_after, OrderState.PARTIAL, first.binding.causal)
            # .8 would fail the admission LOT_SIZE lattice; do not re-admit a remaining fill.
            self.assertNotEqual(math.validate_order(replace(view.admitted.intent, quantity=D(".8")), f.rules.rules, f.context).status, "VALID")
            with patch.object(math, "validate_order", side_effect=AssertionError("dynamic re-admission")):
                self.assertIsNone(f.fill(partial, f.trades[0]))
                last = f.fill(partial, f.trades[1]).proposal
            self.assertEqual(last.fill_quantity, D(".8"))
            self.assertLessEqual(last.fill_quantity, f.trades[1].quantity)
            self.assertEqual(last.remaining_after, D(0))
            self.assertEqual(last.status, "FILLED")
            self.assertEqual(view.remaining_quantity, D("1.1"))
            self.assertEqual(partial.remaining_quantity, D(".8"))
            with self.assertRaises(ValueError):
                OrderExecutionView(view.admitted, D(".8"), OrderState.PARTIAL)

    def test_mark_touch_is_irrelevant_and_passive_does_not_charge_spread_or_slippage(self):
        with fixture([print_row(price="100", aggressor=SELL)]) as f:
            self.assertLess(f.minute.candle.low, D(100))
            self.assertIsNone(f.fill(f.view(market=False)))
            with self.assertRaises(ValueError):
                f.fill(f.view(market=False), f.minute)
        with fixture([print_row(price="99")]) as f:
            unavailable = replace(SIMULATION_EVIDENCE, classification=Provenance.UNAVAILABLE)
            p = FillPolicy(FixedBpsPolicy(D(10), unavailable), FixedBpsPolicy(D(20), unavailable))
            fill = f.fill(f.view(market=False), fill_policy=p).proposal
            self.assertEqual(fill.fill_price, D(100))
            self.assertEqual(fill.fee_role, FeeRole.MAKER)


class ConditionalTests(unittest.TestCase):
    def test_all_four_exact_trigger_inequalities_and_boundary(self):
        for kind, side, passing, failing in ((TriggerType.STOP, BUY, "101", "99"),
                                            (TriggerType.STOP, SELL, "99", "101"),
                                            (TriggerType.TAKE_PROFIT, BUY, "99", "101"),
                                            (TriggerType.TAKE_PROFIT, SELL, "101", "99")):
            with self.subTest(kind=kind, side=side), fixture(
                    [print_row(10, price=passing), print_row(11, price=failing), print_row(12, price="100")]) as f:
                parent = f.conditional(side, kind)
                candidate = f.trigger(parent)
                p = candidate.proposal
                self.assertEqual(p.status, "TRIGGERED_EXACT")
                self.assertEqual(p.next_state, "ACTIVE")
                self.assertEqual(p.trigger_result, math.trigger_predicate(parent.order.trigger, contract_price=D(passing)))
                self.assertEqual(p.binding.causal.source_event_identity, f.trades[0].identity)
                self.assertEqual(p.binding.causal.sequence_key, (TIME, 10))
                self.assertIsNone(f.trigger(parent, f.trades[1]))
                self.assertEqual(f.trigger(parent, f.trades[2]).proposal.trigger_result.value, True)
                child = resolution.activated_child_view(resolution.resolve_causal_frontier((candidate,)), parent)
                self.assertEqual(child.admitted.conditional_parent_identity, parent.order.identity)
                self.assertIsNone(f.fill(child, f.trades[0]))
                self.assertIsInstance(f.fill(child, f.trades[1]).proposal, FillProposal)

    def test_child_admits_at_trigger_time_with_current_context_not_creation_context(self):
        with fixture() as f:
            parent = f.conditional()
            missing = f.trigger(parent, context=False)
            self.assertEqual(missing.proposal.status, "TRIGGERED_EXACT")
            self.assertEqual(missing.proposal.next_state, "TRIGGERED_CHILD_UNAVAILABLE")
            self.assertIsNone(missing.proposal.child_admission.admitted)
            self.assertEqual(resolution.resolve_causal_frontier((missing,)).status, "SELECTED")
            valid = f.trigger(parent).proposal.child_admission.admitted
            self.assertEqual(valid.admission_timestamp_ms, TIME)
            self.assertGreater(valid.admission_timestamp_ms, parent.order.creation.latest_possible_ms)
            self.assertEqual(valid.context, f.context)
            invalid_child = replace(parent, order=replace(parent.order, child_intent=OrderIntent(BUY, D(".3"), None, market=True)))
            self.assertIsNone(f.trigger(invalid_child).proposal.child_admission.admitted)

    def test_mark_high_low_envelopes_have_intervals_and_no_inside_interval_fill(self):
        for kind, side, trigger in ((TriggerType.STOP, BUY, "120"), (TriggerType.STOP, SELL, "90"),
                                    (TriggerType.TAKE_PROFIT, SELL, "120"), (TriggerType.TAKE_PROFIT, BUY, "90")):
            with self.subTest(kind=kind, side=side), fixture(
                    [print_row(10, TIME + 500), print_row(11, TIME + 59999), print_row(12, TIME + 60000)]) as f:
                parent = f.conditional(side, kind, PriceReference.MARK_PRICE, trigger)
                candidate = f.trigger(parent)
                p = candidate.proposal
                self.assertEqual(p.status, "TRIGGERED_WITHIN_INTERVAL")
                self.assertEqual(p.timing_basis, "CROSSED_WITHIN_MARK_MINUTE")
                self.assertFalse(p.opening_trigger_result.value)
                self.assertEqual((candidate.causal.earliest_possible_ms, candidate.causal.latest_possible_ms), (TIME, TIME + 59999))
                self.assertFalse(candidate.causal.exact)
                self.assertIsNone(candidate.causal.sequence_key)
                child = resolution.activated_child_view(resolution.resolve_causal_frontier((candidate,)), parent)
                self.assertEqual(child.admitted.admission_timestamp_ms, TIME + 59999)
                self.assertIsNone(f.fill(child, f.trades[0]))
                self.assertIsNone(f.fill(child, f.trades[1]))
                self.assertIsInstance(f.fill(child, f.trades[2]).proposal, FillProposal)
                unhit = replace(parent, order=replace(parent.order, trigger=replace(parent.order.trigger,
                    price=D(140) if side is BUY and kind is TriggerType.STOP or side is SELL and kind is TriggerType.TAKE_PROFIT else D(70))))
                self.assertIsNone(f.trigger(unhit))

    def test_mark_condition_already_true_at_open_uses_creation_to_open_bounds(self):
        cases = ((TriggerType.STOP, BUY, "80"), (TriggerType.STOP, SELL, "90"),
                 (TriggerType.TAKE_PROFIT, BUY, "90"), (TriggerType.TAKE_PROFIT, SELL, "80"))
        for kind, side, trigger in cases:
            with self.subTest(kind=kind, side=side), fixture(
                    [print_row(10, TIME), print_row(11, TIME + 1)], mark_prices=("85", "100", "70", "85")) as f:
                parent = f.conditional(side, kind, PriceReference.MARK_PRICE, trigger)
                candidate = f.trigger(parent)
                proposal = candidate.proposal
                self.assertEqual(proposal.status, "TRIGGERED_BY_MARK_OPEN")
                self.assertEqual(proposal.timing_basis, "CONDITION_ALREADY_TRUE_AT_MARK_OPEN")
                self.assertTrue(proposal.opening_trigger_result.value)
                self.assertEqual((candidate.causal.earliest_possible_ms, candidate.causal.latest_possible_ms),
                                 (parent.order.creation.earliest_possible_ms, TIME))
                self.assertEqual(proposal.child_admission.admitted.activation, candidate.causal)
                child = resolution.activated_child_view(resolution.resolve_causal_frontier((candidate,)), parent)
                self.assertIsNone(f.fill(child, f.trades[0]))
                self.assertIsInstance(f.fill(child, f.trades[1]).proposal, FillProposal)

    def test_exact_known_trigger_stays_selected_when_child_is_rejected_or_unavailable(self):
        with fixture() as f:
            parent = f.conditional()
            rejected_parent = replace(parent, order=replace(parent.order,
                child_intent=OrderIntent(BUY, D(".3"), None, market=True)))
            rejected = f.trigger(rejected_parent)
            self.assertEqual(rejected.proposal.status, "TRIGGERED_EXACT")
            self.assertEqual(rejected.proposal.child_admission.status, "REJECTED_RULE")
            self.assertEqual(rejected.proposal.next_state, "TRIGGERED_CHILD_REJECTED")
            selected_rejected = resolution.resolve_causal_frontier((rejected,))
            self.assertEqual(selected_rejected.status, "SELECTED")
            self.assertEqual(selected_rejected.proposals[0].child_admission.status, "REJECTED_RULE")
            with self.assertRaises(ValueError):
                resolution.activated_child_view(selected_rejected, rejected_parent)

            unavailable = f.trigger(parent, context=False)
            self.assertEqual(unavailable.proposal.status, "TRIGGERED_EXACT")
            self.assertEqual(unavailable.proposal.child_admission.status, "UNAVAILABLE_RULE")
            self.assertEqual(unavailable.proposal.next_state, "TRIGGERED_CHILD_UNAVAILABLE")
            selected_unavailable = resolution.resolve_causal_frontier((unavailable,))
            self.assertEqual(selected_unavailable.status, "SELECTED")
            self.assertEqual(selected_unavailable.proposals[0].child_admission.status, "UNAVAILABLE_RULE")
            with self.assertRaises(ValueError):
                resolution.activated_child_view(selected_unavailable, parent)

    def test_price_protection_and_creation_overlap_remain_explicit(self):
        with fixture() as f:
            protected = f.trigger(f.conditional(price_protect=True))
            self.assertEqual(protected.proposal.status, "UNSUPPORTED_PRICE_PROTECTION")
            parent = f.conditional(reference=PriceReference.MARK_PRICE)
            overlap = replace(parent, order=replace(parent.order, creation=resolution.external_boundary(TIME + 500, HASH)))
            candidate = f.trigger(overlap)
            self.assertEqual(candidate.proposal.status, "AMBIGUOUS_TRIGGER_ACTIVATION_ORDER")
            self.assertEqual(candidate.causal.earliest_possible_ms, TIME + 500)
            self.assertEqual(candidate.causal.latest_possible_ms, TIME + 59999)
            self.assertEqual(resolution.resolve_causal_frontier((candidate,)).status, "AMBIGUOUS")
            cancelled = replace(parent, state="CANCELLED")
            self.assertIsNone(f.trigger(cancelled))


class CausalAndAmbiguityTests(unittest.TestCase):
    def test_partial_order_intervals_keys_and_cross_stream_equality(self):
        a = CausalBounds(TIME, TIME + 10, "mark", HASH)
        b = CausalBounds(TIME + 11, TIME + 20, "mark", "b" * 64)
        self.assertTrue(resolution.definitely_precedes(a, b))
        self.assertFalse(resolution.definitely_precedes(b, a))
        t1 = CausalBounds(TIME, TIME, f"aggTrades:{INSTRUMENT}", HASH, (TIME, 10))
        t2 = replace(t1, source_event_identity="b" * 64, sequence_key=(TIME, 11))
        self.assertTrue(resolution.definitely_precedes(t1, t2))
        funding_time = CausalBounds(TIME, TIME, f"funding:{INSTRUMENT}", "c" * 64)
        self.assertFalse(resolution.definitely_precedes(t1, funding_time))
        self.assertFalse(resolution.definitely_precedes(funding_time, t1))
        self.assertFalse(resolution.definitely_precedes(t1, a))
        self.assertFalse(resolution.definitely_precedes(a, t1))

    def test_frontier_is_independent_of_input_order_without_event_priority(self):
        with fixture([print_row(10), print_row(11, TIME + 1)]) as f:
            first = f.fill(f.view(), f.trades[0])
            second = f.fill(f.view(), f.trades[1])
            a = resolution.resolve_causal_frontier((second, first))
            b = resolution.resolve_causal_frontier((first, second))
            self.assertEqual(a, b)
            self.assertEqual(a.status, "SELECTED")
            self.assertEqual(a.proposals, (first.proposal,))
            self.assertEqual(set(a.candidate_identities), {first.identity, second.identity})

    def test_stop_or_target_vs_liquidation_same_mark_envelope_is_ambiguous(self):
        with fixture() as f:
            risk = f.risk()
            for kind, price in ((TriggerType.STOP, "90"), (TriggerType.TAKE_PROFIT, "120")):
                parent = f.conditional(SELL, kind, PriceReference.MARK_PRICE, price)
                trigger = f.trigger(parent)
                a = resolution.resolve_causal_frontier((risk, trigger))
                b = resolution.resolve_causal_frontier((trigger, risk))
                self.assertEqual(a, b)
                self.assertEqual(a.status, "AMBIGUOUS")
                self.assertEqual(a.proposals, ())
                self.assertEqual(set(a.candidate_identities), {risk.identity, trigger.identity})
                with self.assertRaises(ValueError):
                    resolution.activated_child_view(a, parent)
                with self.assertRaises(ValueError):
                    resolution.liquidation_closeout(a, f.position(), CloseoutPolicy(policy().spread, D(".01"), SIMULATION_EVIDENCE))

    def test_funding_and_position_changing_fill_at_same_time_are_ambiguous(self):
        with fixture() as f:
            fill = f.fill(f.view())
            funding_event = resolution.funding_candidate(f.position(), f.event, f.funding, f.evidence)
            for candidates in ((fill, funding_event), (funding_event, fill)):
                result = resolution.resolve_causal_frontier(candidates)
                self.assertEqual(result.status, "AMBIGUOUS")
                self.assertEqual(result.proposals, ())
                self.assertEqual(len(result.frontier), 2)

    def test_exact_trade_inside_mark_interval_is_incomparable_and_ambiguous(self):
        with fixture([print_row(timestamp=TIME + 500)]) as f:
            fill, risk = f.fill(f.view()), f.risk()
            self.assertFalse(resolution.definitely_precedes(fill.causal, risk.causal))
            self.assertFalse(resolution.definitely_precedes(risk.causal, fill.causal))
            self.assertEqual(resolution.resolve_causal_frontier((fill, risk)).status, "AMBIGUOUS")

    def test_competing_passive_orders_share_print_volume_without_double_allocation(self):
        with fixture([print_row(price="99")]) as f:
            first = f.fill(f.view(market=False))
            other = f.view(market=False, order_id="order-2")
            second = f.fill(other)
            self.assertEqual(resolution.resolve_causal_frontier((first, second)).status, "AMBIGUOUS")

    def test_independent_minimal_candidates_are_retained_and_duplicates_collapse(self):
        with fixture() as f:
            first = f.fill(f.view())
            other = f.view(order_id="order-2")
            other = replace(other, admitted=replace(other.admitted, position_id="position-2"))
            second = f.fill(other)
            selected = resolution.resolve_causal_frontier((first, second, first))
            self.assertEqual(selected.status, "SELECTED")
            self.assertEqual(len(selected.frontier), 2)
            self.assertEqual({p.identity for p in selected.proposals}, {first.proposal.identity, second.proposal.identity})
            self.assertEqual(selected, resolution.resolve_causal_frontier((second, first)))

    def test_missing_funding_mark_does_not_silently_prioritize_same_time_fill(self):
        with fixture() as f:
            event = funding()
            collection = FundingEvidence(SYMBOL, INSTRUMENT, (event,), HASH)
            evidence = execution_evidence_snapshot(SCOPE, contract_rules=f.rules, aggtrades=f.tape,
                                                   fees=f.fees, funding=collection)
            fund = resolution.funding_candidate(f.position(), event, collection, evidence)
            fill = resolution.fill_candidate(f.view(), f.trades[0], f.tape, f.fees, policy(), evidence)
            self.assertEqual(fund.proposal.status, "UNAVAILABLE_FUNDING_MARK")
            result = resolution.resolve_causal_frontier((fund, fill))
            self.assertEqual(result.status, "AMBIGUOUS")
            self.assertEqual(result.proposals, ())


class FundingTests(unittest.TestCase):
    def test_exact_settlement_amount_signs_quantity_and_no_leverage_factor(self):
        with fixture() as f:
            for side in (LONG, SHORT):
                for rate in (D(".0001"), D("-.0001")):
                    base = replace(funding(), funding_rate=rate)
                    event = join_settlement_mark(base, exact_mark(base))
                    collection = FundingEvidence(SYMBOL, INSTRUMENT, (event,), HASH)
                    evidence = execution_evidence_snapshot(SCOPE, funding=collection)
                    position = f.position(side, quantity=".75")
                    candidate = resolution.funding_candidate(position, event, collection, evidence)
                    selected = resolution.resolve_causal_frontier((candidate,))
                    p = selected.proposals[0]
                    self.assertIsInstance(p, FundingProposal)
                    self.assertEqual(p.quantity_at_settlement, D(".75"))
                    self.assertEqual(p.cashflow, math.funding(side, D(".75"), rate, D(100)))
                    self.assertNotIn("leverage", {field.name for field in fields(p)})

    def test_missing_mark_and_unconfirmed_position_quantity_remain_unavailable(self):
        with fixture() as f:
            event = funding()
            collection = FundingEvidence(SYMBOL, INSTRUMENT, (event,), HASH)
            evidence = execution_evidence_snapshot(SCOPE, funding=collection)
            candidate = resolution.funding_candidate(f.position(), event, collection, evidence)
            self.assertEqual(candidate.proposal.status, "UNAVAILABLE_FUNDING_MARK")
            self.assertEqual(candidate.proposal.calculation.status, "UNAVAILABLE_FUNDING_MARK")
            self.assertEqual(resolution.resolve_causal_frontier((candidate,)).status, "UNAVAILABLE")
            unknown = replace(f.position(), effective_through_ms=None)
            self.assertEqual(resolution.funding_candidate(unknown, f.event, f.funding, f.evidence).proposal.status,
                             "UNAVAILABLE_POSITION_AT_SETTLEMENT")
            self.assertNotEqual(candidate.proposal_id, replace(candidate, proposal=replace(candidate.proposal,
                binding=replace(candidate.proposal.binding, input_identity="c" * 64))).proposal_id)

    def test_exact_charges_on_same_confirmed_quantity_commute_without_funding_priority(self):
        with fixture() as f:
            special = replace(funding(), funding_rate=D("-.0002"), factual_fields_json='{"rateType":"Special"}')
            special = join_settlement_mark(special, exact_mark(special))
            collection = FundingEvidence(SYMBOL, INSTRUMENT, (f.event, special), HASH)
            evidence = execution_evidence_snapshot(SCOPE, funding=collection)
            position = f.position()
            a = resolution.funding_candidate(position, f.event, collection, evidence)
            b = resolution.funding_candidate(position, special, collection, evidence)
            self.assertFalse(resolution.definitely_precedes(a.causal, b.causal))
            self.assertFalse(resolution.definitely_precedes(b.causal, a.causal))
            self.assertEqual(resolution.resolve_causal_frontier((a, b)).status, "SELECTED")
            self.assertEqual(resolution.resolve_causal_frontier((a, b)), resolution.resolve_causal_frontier((b, a)))
            changed = resolution.funding_candidate(replace(position, quantity=D(1)), special, collection, evidence)
            self.assertNotEqual(b.proposal.proposal_id, changed.proposal.proposal_id)
            self.assertEqual(resolution.resolve_causal_frontier((a, changed)).status, "AMBIGUOUS")


class LiquidationTests(unittest.TestCase):
    def test_long_low_short_high_cross_exact_part1_threshold_without_crossing_time(self):
        with fixture() as f:
            for side in (LONG, SHORT):
                position = f.position(side)
                risk = f.risk(position)
                p = risk.proposal
                self.assertIsInstance(p, LiquidationRiskProposal)
                expected = math.liquidation_threshold(side, position.quantity, position.average_entry,
                                                      position.isolated_wallet_collateral, f.brackets.table)
                self.assertEqual(p.threshold, expected)
                self.assertEqual(p.threshold.bracket_identity, expected.bracket_identity)
                self.assertFalse(risk.causal.exact)
                self.assertEqual((risk.causal.earliest_possible_ms, risk.causal.latest_possible_ms), (TIME, TIME + 59999))
                self.assertIsNone(risk.causal.sequence_key)
        with fixture(mark_prices=("100", "101", "99", "100")) as f:
            self.assertIsNone(f.risk(f.position(LONG)))
            self.assertIsNone(f.risk(f.position(SHORT)))

    def test_mark_open_already_breached_uses_position_start_to_open_bounds(self):
        for side in (LONG, SHORT):
            with self.subTest(side=side):
                open_price = D(89) if side is LONG else D(111)
                high, low = max(D(100), open_price + 1), min(D(100), open_price - 1)
            with fixture(mark_prices=(str(open_price), str(high), str(low), "100")) as f:
                position = f.position(side)
                risk = f.risk(position)
                self.assertIsInstance(risk.proposal, LiquidationRiskProposal)
                self.assertEqual(risk.proposal.crossing_basis, "BREACHED_BY_MARK_OPEN")
                self.assertEqual((risk.causal.earliest_possible_ms, risk.causal.latest_possible_ms),
                                 (position.effective_from_ms, TIME))
                only_through_open = replace(position, effective_through_ms=TIME)
                still_known = f.risk(only_through_open)
                self.assertIsInstance(still_known.proposal, LiquidationRiskProposal)
                self.assertEqual(still_known.causal, risk.causal)

    def test_mark_open_breach_remains_incomparable_with_preopen_funding(self):
        with fixture(mark_prices=("80", "100", "70", "100")) as f:
            original = funding()
            settlement = TIME - 1
            event_source = replace(original.source, provenance=replace(
                original.source.provenance, effective_at_ms=settlement))
            early_event = replace(original, funding_timestamp_ms=settlement,
                                  available_at_ms=TIME, source=event_source)
            mark = exact_mark(early_event)
            mark_source = replace(mark.source, provenance=replace(
                mark.source.provenance, effective_at_ms=settlement))
            early_event = join_settlement_mark(early_event, replace(
                mark, source=mark_source, funding_timestamp_ms=settlement,
                funding_event_identity=early_event.event_identity))
            collection = FundingEvidence(SYMBOL, INSTRUMENT, (early_event,), HASH)
            evidence = execution_evidence_snapshot(SCOPE, contract_rules=f.rules, fees=f.fees,
                brackets=f.brackets, aggtrades=f.tape, mark_price=f.marks, funding=collection)
            position = f.position()
            risk = f.risk(position)
            charge = resolution.funding_candidate(position, early_event, collection, evidence)
            self.assertEqual(risk.proposal.crossing_basis, "BREACHED_BY_MARK_OPEN")
            self.assertFalse(resolution.definitely_precedes(charge.causal, risk.causal))
            self.assertFalse(resolution.definitely_precedes(risk.causal, charge.causal))
            self.assertEqual(resolution.resolve_causal_frontier((charge, risk)).status, "AMBIGUOUS")

    def test_breached_open_with_position_starting_later_is_unavailable_without_closeout(self):
        with fixture(mark_prices=("80", "100", "70", "100")) as f:
            position = replace(f.position(), effective_from_ms=TIME + 500,
                               effective_through_ms=TIME + 120000)
            candidate = f.risk(position)
            self.assertIsInstance(candidate.proposal, UnavailableProposal)
            self.assertEqual(candidate.proposal.status, "UNAVAILABLE_POSITION_AT_BREACHED_MARK_OPEN")
            self.assertEqual((candidate.causal.earliest_possible_ms, candidate.causal.latest_possible_ms),
                             (TIME, TIME + 59999))
            resolution_result = resolution.resolve_causal_frontier((candidate,))
            self.assertEqual(resolution_result.status, "UNAVAILABLE")
            closeout = CloseoutPolicy(FixedBpsPolicy(D(25), SIMULATION_EVIDENCE),
                                      D(".01"), SIMULATION_EVIDENCE)
            with self.assertRaises(ValueError):
                resolution.liquidation_closeout(resolution_result, position, closeout)

    def test_position_entirely_outside_mark_minute_has_no_liquidation_candidate(self):
        with fixture(mark_prices=("80", "100", "70", "100")) as f:
            ended_before = replace(f.position(), effective_through_ms=TIME - 1)
            starts_after = replace(f.position(), effective_from_ms=TIME + 60000,
                                   effective_through_ms=TIME + 120000)
            self.assertIsNone(f.risk(ended_before))
            self.assertIsNone(f.risk(starts_after))

    def test_unavailable_brackets_and_position_window_and_zero_position(self):
        with fixture() as f:
            unknown = normalize_bracket_snapshot(encoded(bracket_payload()), SYMBOL,
                current(classification=Provenance.UNAVAILABLE), account_specific=False, values_basis="EFFECTIVE_TIERS")
            evidence = execution_evidence_snapshot(SCOPE, brackets=unknown, mark_price=f.marks)
            risk = resolution.liquidation_candidate(f.position(), f.minute, f.marks, unknown, evidence)
            self.assertIsInstance(risk.proposal, UnavailableProposal)
            self.assertIsNone(f.risk(f.position(quantity="0")))
            self.assertEqual(f.risk(replace(f.position(), effective_through_ms=None)).proposal.status,
                             "UNAVAILABLE_POSITION_DURING_MARK_INTERVAL")

    def test_closeout_only_selected_risk_has_adverse_price_and_separate_exact_charge(self):
        with fixture() as f:
            closeout = CloseoutPolicy(FixedBpsPolicy(D(25), SIMULATION_EVIDENCE), D(".01"), SIMULATION_EVIDENCE)
            for side in (LONG, SHORT):
                position = f.position(side)
                risk = f.risk(position)
                selected = resolution.resolve_causal_frontier((risk,))
                with localcontext() as context:
                    context.prec = 1
                    proposal = resolution.liquidation_closeout(selected, position, closeout)
                    closing = SELL if side is LONG else BUY
                    expected = math.adverse_price(risk.proposal.threshold.value, closing, closeout.adverse_bps)
                    self.assertIsInstance(proposal, LiquidationCloseoutProposal)
                    self.assertEqual(proposal.closing_side, closing)
                    self.assertEqual(proposal.reference_threshold, risk.proposal.threshold.value)
                    self.assertEqual(proposal.simulated_closeout_price, expected)
                    self.assertEqual(exact_scalar(proposal.closeout_charge),
                        exact_scalar(position.quantity) * exact_scalar(expected.value) * exact_scalar(closeout.fee_rate))
                    self.assertIsNone(proposal.normal_trading_fee)
                    self.assertEqual(proposal.classification, Provenance.FIXED_SIMULATION_ASSUMPTION)
                    self.assertEqual(proposal.risk_proposal_identity, risk.proposal.identity)
                    self.assertEqual(proposal.selected_candidate_selection_identity,
                                     selected.candidate_selection_identity(risk))
                    self.assertEqual(proposal.fee_evidence_identity, closeout.fee_evidence.identity)
                self.assertEqual(proposal.proposal_id, resolution.liquidation_closeout(selected, position, closeout).proposal_id)
                changed = replace(closeout, fee_rate=D(".02"))
                self.assertNotEqual(proposal.proposal_id, resolution.liquidation_closeout(selected, position, changed).proposal_id)
                with self.assertRaises(ValueError):
                    resolution.liquidation_closeout(selected, replace(position, quantity=D(1)), closeout)

    def test_closeout_unavailable_provenance_is_not_normal_fee(self):
        with fixture() as f:
            position = f.position()
            selected = resolution.resolve_causal_frontier((f.risk(position),))
            unknown = replace(SIMULATION_EVIDENCE, classification=Provenance.UNAVAILABLE)
            p = CloseoutPolicy(policy().spread, D(".01"), SIMULATION_EVIDENCE)
            for changed in (replace(p, evidence=unknown), replace(p, fee_evidence=unknown),
                            replace(p, adverse_bps=FixedBpsPolicy(D(0), unknown))):
                result = resolution.liquidation_closeout(selected, position, changed)
                self.assertIsInstance(result, UnavailableProposal)
                self.assertEqual(result.status, "UNAVAILABLE_CLOSEOUT_POLICY")

    def test_closeout_identity_ignores_dominated_window_candidates_and_competitors_block_it(self):
        with fixture([print_row(10, TIME + 500), print_row(11, TIME + 60000)]) as f:
            position = f.position()
            risk = f.risk(position)
            closeout = CloseoutPolicy(FixedBpsPolicy(D(25), SIMULATION_EVIDENCE), D(".01"), SIMULATION_EVIDENCE)
            alone = resolution.resolve_causal_frontier((risk,))
            other = f.view(admitted_at=TIME + 59999,
                activation=resolution.external_boundary(TIME + 59999, HASH), order_id="unrelated")
            other = replace(other, admitted=replace(other.admitted, position_id="position-2"))
            later = f.fill(other, f.trades[1])
            with_later = resolution.resolve_causal_frontier((risk, later))
            self.assertEqual(with_later.status, "SELECTED")
            self.assertEqual(with_later.frontier, (risk,))
            self.assertNotEqual(with_later.identity, alone.identity)
            first_closeout = resolution.liquidation_closeout(alone, position, closeout)
            second_closeout = resolution.liquidation_closeout(with_later, position, closeout)
            self.assertEqual(first_closeout.proposal_id, second_closeout.proposal_id)

            independent_view = f.view(order_id="independent")
            independent_view = replace(independent_view,
                admitted=replace(independent_view.admitted, position_id="position-2"))
            independent = f.fill(independent_view, f.trades[0])
            with_independent = resolution.resolve_causal_frontier((risk, independent))
            reversed_input = resolution.resolve_causal_frontier((independent, risk))
            self.assertEqual(with_independent.status, "SELECTED")
            self.assertEqual(len(with_independent.frontier), 2)
            self.assertNotEqual(with_independent.identity, alone.identity)
            self.assertEqual(with_independent, reversed_input)
            self.assertEqual(alone.candidate_selection_identity(risk),
                             with_independent.candidate_selection_identity(risk))
            self.assertEqual(alone.candidate_selection_identity(risk),
                             reversed_input.candidate_selection_identity(risk))
            third_closeout = resolution.liquidation_closeout(with_independent, position, closeout)
            self.assertEqual(first_closeout.proposal_id, third_closeout.proposal_id)

            competing = f.fill(f.view(), f.trades[0])
            ambiguous = resolution.resolve_causal_frontier((risk, competing))
            self.assertEqual(ambiguous.status, "AMBIGUOUS")
            with self.assertRaises(ValueError):
                resolution.liquidation_closeout(ambiguous, position, closeout)


class StreamingAndBoundaryTests(unittest.TestCase):
    def test_production_tape_iterator_is_consumed_lazily_and_closed(self):
        with fixture([print_row(10, TIME - 2), print_row(11), print_row(12, TIME + 1)]) as f:
            seen, closed = [], []
            def bounded_source(root, tape):
                try:
                    for i, row in enumerate(f.trades):
                        if i == 2:
                            raise AssertionError("consumed past first eligible fill")
                        seen.append(row.aggregate_trade_id)
                        yield row
                finally:
                    closed.append(True)
            with patch.object(resolution, "iter_execution_trades", new=bounded_source):
                candidate = resolution.next_tape_fill_candidate(f.root, f.view(), f.tape, f.fees, policy(), f.evidence)
            self.assertEqual(candidate.causal.sequence_key, (TIME, 11))
            self.assertEqual(seen, [10, 11])
            self.assertEqual(closed, [True])
            real = resolution.next_tape_fill_candidate(f.root, f.view(), f.tape, f.fees, policy(), f.evidence)
            self.assertEqual(real, candidate)

    def test_small_frontier_capacity_prevents_global_event_materialization(self):
        with fixture() as f:
            candidate = f.fill(f.view())
            seen = [0]
            def too_many():
                for i in range(MAX_FRONTIER_CANDIDATES + 100):
                    seen[0] += 1
                    yield replace(candidate, resource_ids=(f"independent-{i}",))
            with self.assertRaisesRegex(ValueError, "bounded frontier capacity"):
                resolution.resolve_causal_frontier(too_many())
            self.assertEqual(seen[0], MAX_FRONTIER_CANDIDATES + 1)

    def test_terminal_views_never_consume_historical_rows(self):
        with fixture() as f:
            view = f.view()
            fill = f.fill(view).proposal
            finished = OrderExecutionView(view.admitted, D(0), OrderState.FILLED, fill.binding.causal)
            cancelled = replace(view, state=OrderState.CANCELLED)
            with patch.object(resolution, "iter_execution_trades", side_effect=AssertionError("terminal tape opened")):
                for terminal in (finished, cancelled):
                    self.assertIsNone(resolution.next_tape_fill_candidate(f.root, terminal, f.tape, f.fees, policy(), f.evidence))
            self.assertEqual(view.remaining_quantity, D(2))

    def test_wrong_scope_evidence_and_noncanonical_stream_rejected(self):
        with fixture([print_row(10), print_row(11)]) as f:
            wrong = execution_evidence_snapshot(SCOPE, contract_rules=f.rules, fees=f.fees)
            with self.assertRaises(ValueError):
                resolution.fill_candidate(f.view(), f.trades[0], f.tape, f.fees, policy(), wrong)
            with self.assertRaises(ValueError):
                resolution.next_fill_candidate(f.view(admitted_at=TIME + 1), iter(reversed(f.trades)), f.tape, f.fees, policy(), f.evidence)
            self.assertEqual(resolution.resolve_causal_frontier(()).status, "NO_ACTION")


if __name__ == "__main__":
    unittest.main()
