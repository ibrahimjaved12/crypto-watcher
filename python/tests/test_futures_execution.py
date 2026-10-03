"""Focused frozen #37 Part 1 deterministic execution fixtures."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
import unittest

from market_analysis import futures_execution as f
from market_analysis.futures_execution_contracts import (
    ALGORITHM_VERSION, Bracket, BracketTable, ContractRules, Evidence, FeePolicy, FeeRole,
    FixedBpsPolicy, Grid, MinNotional, OrderIntent, OrderSide, PercentPrice,
    PriceFilter, RuleEvaluationContext,
    PositionSide, PriceReference, Provenance, Scope, Trigger, TriggerType,
)

from market_analysis.exact_scalar import ExactScalar

D = Decimal
LONG, SHORT = PositionSide.LONG, PositionSide.SHORT
BUY, SELL = OrderSide.BUY, OrderSide.SELL
EVIDENCE = Evidence(Provenance.FIXED_SIMULATION_ASSUMPTION, "fixture", "v1")
SCOPE = Scope("binance-usdm:BTCUSDT")
PRICE = PriceFilter(D('1'), D('10000'), D('.25'), EVIDENCE)
LOT = Grid(D('.1'), D('100'), D('.1'), EVIDENCE)
MARKET_LOT = replace(LOT, minimum=D('.2'), maximum=D('10'), increment=D('.2'))
RULES = ContractRules(SCOPE, EVIDENCE, "TRADING", "PERPETUAL", PRICE, LOT,
                      MARKET_LOT, MinNotional(D('5'), True, EVIDENCE))
TABLE = BracketTable(SCOPE, (
    Bracket('1', D(0), D(1000), 20, D('.01'), D(0)),
    Bracket('2', D(1000), D(10000), 10, D('.02'), D(10)),
), EVIDENCE)
FEES = FeePolicy(D('.0002'), D('.0005'), EVIDENCE)


def intent(**changes):
    return replace(OrderIntent(BUY, D('1'), D('100')), **changes)


class ExactScalarTests(unittest.TestCase):
    def test_reduced_identity_finite_views_and_safe_boundaries(self):
        self.assertEqual(ExactScalar(2, -6), ExactScalar(-1, 3))
        self.assertEqual(ExactScalar(0, -6).as_integer_ratio(), (0, 1))
        self.assertEqual(ExactScalar(2, 6).identity, ExactScalar(1, 3).identity)
        self.assertNotEqual(ExactScalar(1, 3).identity, ExactScalar(2, 3).identity)
        for decimal in (D('12345.123456789'), D('-0.125'), D('1E+100'), D(0)):
            self.assertEqual(ExactScalar.from_decimal(decimal).decimal_value, decimal)
        self.assertIsNone(ExactScalar(1, 3).decimal_value)
        self.assertEqual(ExactScalar(1, 3) + ExactScalar(2, 3), D(1))
        self.assertEqual(D(1) - ExactScalar(1, 3), ExactScalar(2, 3))
        self.assertEqual(ExactScalar(1, 3) * D(3), D(1))
        self.assertEqual(D(1) / ExactScalar(3), ExactScalar(1, 3))
        self.assertEqual(hash(ExactScalar(1, 2)), hash(D('.5')))
        for n, d in ((True, 1), (1, True), (1.0, 3), (1, D(3)), (1, 0)):
            with self.assertRaises(ValueError):
                ExactScalar(n, d)
        with self.assertRaises(FrozenInstanceError):
            ExactScalar(1, 3).numerator = 2
        with self.assertRaises(TypeError):
            ExactScalar(1, 3) + 0.5
        with localcontext() as context:
            context.prec = 1
            self.assertEqual(ExactScalar(123456789, 8).decimal_value, D('15432098.625'))


class PnlTests(unittest.TestCase):
    def test_notional_and_four_pnl_cases(self):
        self.assertEqual(f.notional(D('2'), D('105')).value, D(210))
        for side, mark, expected in ((LONG, '110', '20'), (LONG, '90', '-20'),
                                     (SHORT, '110', '-20'), (SHORT, '90', '20')):
            self.assertEqual(f.unrealized_pnl(side, D(2), D(100), D(mark)).value, D(expected))

    def test_increase_and_partial_reduction(self):
        result = f.increase(D(2), D(100), D(2), D(120))
        self.assertEqual(result.quantity, D(4))
        self.assertEqual(result.entry_result.value, D(110))
        for side, expected in ((LONG, D(20)), (SHORT, D(-20))):
            reduced = f.reduce(side, D(3), D(100), D(1), D(120))
            self.assertEqual((reduced.quantity, reduced.entry, reduced.realized_pnl),
                             (D(2), D(100), expected))
        self.assertIsNone(f.reduce(LONG, D(1), D(100), D(1), D(120)).entry)
        with self.assertRaises(ValueError):
            f.reduce(LONG, D(1), D(100), D(2), D(120))

    def test_exact_division_and_context_independence(self):
        result = f.increase(D(2), D(100), D(1), D(101)).entry_result
        self.assertEqual(result.status, 'VALID')
        self.assertEqual(result.value, ExactScalar(301, 3))
        self.assertEqual(result.exact_value, ExactScalar(301, 3))
        self.assertIsNone(result.decimal_value)
        with localcontext() as context:
            context.prec = 2
            context.rounding = 'ROUND_UP'
            self.assertEqual(f.notional(D('123456789.123456789'), D('10.25')).value,
                             D('1265432088.51543208725'))
            self.assertEqual(f.initial_margin(D('10'), 8).value, D('1.25'))

    def test_repeating_entry_flows_through_downstream_arithmetic(self):
        increase = f.increase(D(2), D(100), D(1), D(101))
        entry = increase.entry_result.value
        self.assertEqual(entry, ExactScalar(301, 3))
        self.assertEqual(increase.entry_result.status, 'VALID')
        self.assertEqual(f.unrealized_pnl(LONG, D(3), entry, D(110)).value, D(29))
        self.assertEqual(f.unrealized_pnl(SHORT, D(3), entry, D(110)).value, D(-29))
        reduction = f.reduce(LONG, D(3), entry, D(1), D(110))
        self.assertEqual(reduction.realized_pnl, ExactScalar(29, 3))
        self.assertEqual(reduction.entry, entry)
        self.assertEqual(f.reduce(SHORT, D(3), entry, D(1), D(110)).realized_pnl, ExactScalar(-29, 3))
        next_increase = f.increase(D(3), entry, D(1), D(103))
        self.assertEqual((next_increase.quantity, next_increase.entry_result.value), (D(4), D(101)))
        self.assertEqual(f.isolated_equity(LONG, D(3), entry, D(110), D(20)).value, D(49))
        root = f.liquidation_threshold(LONG, D(3), entry, D(30), TABLE)
        self.assertEqual(root.status, 'VALID')
        self.assertEqual(root.value, ExactScalar(27100, 297))
        equity = f.isolated_equity(LONG, D(3), entry, root.value, D(30)).value
        maintenance = f.maintenance_margin(f.notional(D(3), root.value).value, TABLE).value
        self.assertEqual(equity, maintenance)
        with localcontext() as context:
            context.prec = 1
            context.rounding = 'ROUND_DOWN'
            self.assertEqual(root, f.liquidation_threshold(LONG, D(3), entry, D(30), TABLE))
            self.assertEqual(f.increase(D(3), entry, D(1), D(103)), next_increase)

    def test_financial_inputs_reject_bool_float_nonfinite(self):
        for invalid in (True, 1, 1.2, D('NaN'), D('Infinity'), D('-Infinity')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                f.notional(invalid, D(100))
        with self.assertRaises(ValueError):
            f.notional(D(-1), D(100))
        with self.assertRaises(ValueError):
            f.unrealized_pnl('LONG', D(1), D(100), D(110))


class CostTests(unittest.TestCase):
    def test_fee_role_and_partial_fills_no_leverage_input(self):
        self.assertEqual(f.fee(D('.3'), D(100), FeeRole.MAKER, FEES).value, D('.006'))
        self.assertEqual(f.fee(D('.3'), D(100), FeeRole.TAKER, FEES).value, D('.015'))
        self.assertEqual(f.fee(D('.3'), D(100), FeeRole.TAKER, FEES).value
                         + f.fee(D('.7'), D(100), FeeRole.TAKER, FEES).value,
                         f.fee(D(1), D(100), FeeRole.TAKER, FEES).value)
        with self.assertRaises(TypeError):
            f.fee(D(1), D(100), FeeRole.MAKER, FEES, leverage=D(10))

    def test_funding_signs_missing_mark_and_no_leverage_input(self):
        for side, rate, expected in ((LONG, '.001', '-.2'), (SHORT, '.001', '.2'),
                                     (LONG, '-.001', '.2'), (SHORT, '-.001', '-.2')):
            self.assertEqual(f.funding(side, D(2), D(rate), D(100)).value, D(expected))
        self.assertEqual(f.funding(LONG, D(1), D('.001'), None).status,
                         'UNAVAILABLE_FUNDING_MARK')
        self.assertIsNone(f.funding(LONG, D(0), D('.001'), None).value)
        with self.assertRaises(TypeError):
            f.funding(LONG, D(1), D('.001'), D(100), leverage=D(20))
        missing = replace(EVIDENCE, classification=Provenance.UNAVAILABLE)
        self.assertEqual(f.fee(D(1), D(100), FeeRole.MAKER, replace(FEES, evidence=missing)).status,
                         'UNAVAILABLE_RULE')


class RuleTests(unittest.TestCase):
    def test_grids_bounds_and_non_power_of_ten(self):
        self.assertEqual(f.validate_order(intent(), RULES).status, 'VALID')
        self.assertEqual(f.price_filter_reasons(D('100.1'), PRICE), ('OFF_GRID',))
        shifted = replace(PRICE, min_price=D('.1'))
        self.assertEqual(f.price_filter_reasons(D('100.1'), shifted), ())
        self.assertEqual(f.price_filter_reasons(D('100'), shifted), ('OFF_GRID',))
        for value, expected in (('0', 'BELOW_MINIMUM'), ('10000.25', 'ABOVE_MAXIMUM')):
            self.assertIn(expected, f.price_filter_reasons(D(value), PRICE))
        self.assertEqual(f.validate_order(intent(quantity=D('.15'), price=D('100.1')), RULES).reasons,
                         ('LOT_SIZE:OFF_GRID', 'PRICE_FILTER:OFF_GRID'))
        self.assertIn('LOT_SIZE:ABOVE_MAXIMUM', f.validate_order(intent(quantity=D(101)), RULES).reasons)

    def test_quantity_minimum_is_lattice_origin_for_both_filters(self):
        lot = Grid(D('.15'), D(100), D('.10'), EVIDENCE)
        market_lot = replace(lot, maximum=D(10))
        rules = replace(RULES, lot=lot, market_lot=market_lot)
        context = RuleEvaluationContext(D(100), EVIDENCE)
        for quantity, valid in (('.15', True), ('.25', True), ('.20', False), ('.30', False)):
            with self.subTest(quantity=quantity):
                expected = () if valid else ('OFF_GRID',)
                self.assertEqual(f.grid_reasons(D(quantity), lot), expected)
                for market in (False, True):
                    order = intent(quantity=D(quantity), market=market, price=None if market else D(100))
                    result = f.validate_order(order, rules, context)
                    self.assertEqual(result.status, 'VALID' if valid else 'REJECTED_RULE')
                    if not valid:
                        self.assertIn('LOT_SIZE:OFF_GRID', result.reasons)
                        if market:
                            self.assertIn('MARKET_LOT_SIZE:OFF_GRID', result.reasons)
        for grid in (lot, market_lot):
            for original, expected in (('.15', '.15'), ('.20', '.15'), ('.25', '.25'),
                                        ('.30', '.25'), ('.34', '.25')):
                suggestion = f.suggest_quantity(D(original), grid)
                self.assertEqual(suggestion.proposed, D(expected))
                self.assertLessEqual(suggestion.proposed, suggestion.original)
                self.assertEqual(f.grid_reasons(suggestion.proposed, grid), ())
                self.assertTrue(suggestion.requires_acceptance)
            too_small = f.suggest_quantity(D('.14'), grid)
            self.assertLessEqual(too_small.proposed, too_small.original)
            self.assertEqual(too_small.status, 'REJECTED_RULE')
            self.assertIn('BELOW_MINIMUM', too_small.reasons)
            self.assertEqual(f.suggest_grid(D('.20'), grid, rounding='CEIL').proposed, D('.25'))
        self.assertIn('ABOVE_MAXIMUM', f.grid_reasons(D('10.05'), market_lot))
        with self.assertRaises(TypeError):
            Grid(D('.15'), D(100), D('.10'), EVIDENCE, origin=D(0))

    def test_order_intent_requires_market_or_price_bearing_shape(self):
        for side in (BUY, SELL):
            with self.assertRaises(ValueError):
                OrderIntent(side, D(1), D(100), market=True)
            with self.assertRaises(ValueError):
                OrderIntent(side, D(1), None, market=False)
            market = OrderIntent(side, D(1), None, market=True)
            self.assertEqual(f.validate_order(market, RULES).status, 'UNAVAILABLE_RULE')
            context = RuleEvaluationContext(D(100), EVIDENCE)
            self.assertEqual(f.validate_order(market, RULES, context).status, 'VALID')
            price_bearing = OrderIntent(side, D(1), D(100))
            self.assertEqual(f.validate_order(price_bearing, RULES).status, 'VALID')
            # A low dynamic mark cannot override the submitted limit notional.
            self.assertEqual(f.validate_order(price_bearing, RULES, replace(context, mark_price=D(1))).status, 'VALID')

    def test_market_lot_and_explicit_mark_context(self):
        order = intent(quantity=D('.3'), market=True, price=None)
        context = RuleEvaluationContext(D(100), EVIDENCE)
        self.assertIn('MARKET_LOT_SIZE:OFF_GRID', f.validate_order(order, RULES, context).reasons)
        self.assertEqual(f.validate_order(replace(order, quantity=D('.4')), RULES, context).status, 'VALID')
        self.assertIn('UNAVAILABLE_MARK_PRICE', f.validate_order(
            replace(order, quantity=D('.4')), RULES).reasons)
        self.assertIn('MARKET_LOT_SIZE:ABOVE_MAXIMUM', f.validate_order(
            replace(order, quantity=D(11)), RULES, context).reasons)
        self.assertEqual(f.validate_order(order, replace(RULES, market_lot=None), context).status, 'UNAVAILABLE_RULE')
        low_mark = replace(context, mark_price=D(10))
        self.assertIn('BELOW_MIN_NOTIONAL', f.validate_order(replace(order, quantity=D('.4')), RULES, low_mark).reasons)
        self.assertNotEqual(f.validate_order(order, RULES, context).identity,
                            f.validate_order(order, RULES, low_mark).identity)
        # A hybrid MARKET intent is malformed, rather than ignored or judged
        # using its submitted price. Valid MARKET admission still uses mark.
        with self.assertRaises(ValueError):
            replace(order, price=D(100))
        self.assertEqual(f.validate_order(replace(order, quantity=D('.4'), reduce_only=True), RULES).status, 'VALID')
        for field in ('notional_price', 'percent_reference_price'):
            with self.assertRaises(TypeError):
                intent(**{field: D(100)})

    def test_min_notional_exception_only(self):
        order = intent(quantity=D('.1'), price=D(10))
        self.assertEqual(f.validate_order(order, RULES).reasons, ('BELOW_MIN_NOTIONAL',))
        self.assertEqual(f.validate_order(replace(order, reduce_only=True), RULES).status, 'VALID')
        no_exception = replace(RULES, min_notional=replace(RULES.min_notional, reduce_only_exempt=False))
        self.assertIn('BELOW_MIN_NOTIONAL', f.validate_order(replace(order, reduce_only=True), no_exception).reasons)
        self.assertIn('LOT_SIZE:BELOW_MINIMUM', f.validate_order(
            replace(order, quantity=D('.05'), reduce_only=True), RULES).reasons)

    def test_malformed_missing_and_unsupported_rules(self):
        for increment in (D(-1),):
            self.assertEqual(f.validate_order(intent(), replace(RULES, price=replace(PRICE, tick_size=increment))).status,
                             'REJECTED_RULE')
        for increment in (D('NaN'), True):
            with self.assertRaises(ValueError):
                replace(PRICE, tick_size=increment)
        self.assertEqual(f.validate_order(intent(), replace(RULES, price=None)).status, 'UNAVAILABLE_RULE')
        self.assertIn('UNSUPPORTED_STATUS', f.validate_order(intent(), replace(RULES, status='HALT')).reasons)
        self.assertIn('UNSUPPORTED_CONTRACT_TYPE', f.validate_order(intent(), replace(RULES, contract_type='DELIVERY')).reasons)
        for changes in ({'margin_mode': 'CROSS'}, {'position_mode': 'HEDGE'}, {'multi_assets': True},
                        {'portfolio_margin': True}, {'bnb_fee_state': True}, {'real_trading': True},
                        {'authenticated': True}, {'settlement': 'USDC'}, {'instrument_id': 'other:BTCUSDT'}):
            self.assertEqual(f.validate_order(intent(), replace(RULES, scope=replace(SCOPE, **changes))).status,
                             'UNSUPPORTED_SCOPE')

    def test_percent_price_directional_mark_evidence(self):
        rule = PercentPrice(D('.9'), D('1.1'), EVIDENCE)
        rules = replace(RULES, percent_price=rule, percent_price_required=True)
        context = RuleEvaluationContext(D(100), EVIDENCE)
        for side, price, allowed in ((BUY, '80', True), (BUY, '120', False),
                                      (SELL, '120', True), (SELL, '80', False),
                                      (BUY, '110', True), (SELL, '90', True)):
            result = f.validate_order(intent(side=side, price=D(price)), rules, context)
            self.assertEqual('OUTSIDE_PERCENT_PRICE' not in result.reasons, allowed)
            self.assertEqual(result.status, 'VALID' if allowed else 'REJECTED_RULE')
        for missing in (None, replace(context, mark_price=None),
                        replace(context, evidence=replace(EVIDENCE, classification=Provenance.UNAVAILABLE))):
            result = f.validate_order(intent(), rules, missing)
            self.assertEqual(result.status, 'UNAVAILABLE_RULE')
            self.assertIn('UNAVAILABLE_MARK_PRICE', result.reasons)
        self.assertIn('UNAVAILABLE_PERCENT_PRICE', f.validate_order(intent(), replace(rules, percent_price=None), context).reasons)
        self.assertIn('PRICE_FILTER:OFF_GRID', f.validate_order(intent(price=D('80.1')), rules, context).reasons)
        self.assertNotEqual(f.validate_order(intent(), rules, context).identity,
                            f.validate_order(intent(), rules, replace(context, mark_price=D(101))).identity)

    def test_price_filter_disabled_components_independent(self):
        # All eight combinations are valid normalized snapshots. Only enabled
        # components constrain the original submitted order.
        for minimum in (D(0), D('1.1')):
            for maximum in (D(0), D(100)):
                for tick in (D(0), D('.25')):
                    rule = PriceFilter(minimum, maximum, tick, EVIDENCE)
                    rules = replace(RULES, price=rule)
                    expected = []
                    price = D('100.1')
                    if maximum:
                        expected.append('ABOVE_MAXIMUM')
                    if tick:
                        if minimum == 0:
                            expected.append('OFF_GRID')
                    self.assertEqual(f.price_filter_reasons(price, rule), tuple(expected))
                    result = f.validate_order(intent(price=price), rules)
                    self.assertEqual(result.reasons, tuple('PRICE_FILTER:' + r for r in expected))
                    self.assertEqual(result.status, 'REJECTED_RULE' if expected else 'VALID')
        disabled_min = replace(PRICE, min_price=D(0), tick_size=D(0))
        self.assertEqual(f.price_filter_reasons(D('.1'), disabled_min), ())
        self.assertEqual(f.price_filter_reasons(D('.1'), replace(disabled_min, min_price=D(1))), ('BELOW_MINIMUM',))
        disabled_max = replace(PRICE, max_price=D(0))
        self.assertEqual(f.price_filter_reasons(D(10001), disabled_max), ())
        no_tick = replace(PRICE, tick_size=D(0))
        for side in (BUY, SELL):
            suggestion = f.suggest_passive_limit(D('100.1'), side, no_tick)
            self.assertEqual(suggestion.proposed, suggestion.original)
            self.assertEqual(suggestion.status, 'UNCHANGED')
            self.assertEqual(suggestion.reasons, ('PRICE_TICK_DISABLED',))
        no_rules = PriceFilter(D(0), D(0), D(0), EVIDENCE)
        self.assertEqual(f.validate_order(intent(price=D('100.12345')), replace(RULES, price=no_rules)).status, 'VALID')
        shifted = replace(PRICE, min_price=D('1.1'))
        self.assertEqual(f.suggest_passive_limit(D('100.2'), BUY, shifted).proposed, D('100.1'))
        self.assertEqual(f.suggest_passive_limit(D('100.2'), SELL, shifted).proposed, D('100.35'))
        # Quantity filters retain strict nonzero increments and maxima.
        for bad in (replace(LOT, increment=D(0)), replace(LOT, maximum=D(0))):
            self.assertEqual(f.validate_order(intent(), replace(RULES, lot=bad)).status, 'REJECTED_RULE')
        bad = replace(PRICE, min_price=D(2), max_price=D(1))
        self.assertEqual(f.validate_order(intent(), replace(RULES, price=bad)).status, 'REJECTED_RULE')

    def test_suggestions_require_acceptance_and_revalidation(self):
        self.assertEqual(f.suggest_passive_limit(D('100.1'), BUY, PRICE).proposed, D(100))
        self.assertEqual(f.suggest_passive_limit(D('100.1'), SELL, PRICE).proposed, D('100.25'))
        suggestion = f.suggest_quantity(D('.19'), LOT)
        self.assertEqual((suggestion.original, suggestion.proposed, suggestion.requires_acceptance),
                         (D('.19'), D('.1'), True))
        original = intent(quantity=D('.19'), price=D(10))
        self.assertEqual(f.validate_order(original, RULES).status, 'REJECTED_RULE')
        self.assertIn('BELOW_MIN_NOTIONAL', f.validate_order(replace(original, quantity=suggestion.proposed), RULES).reasons)
        tiny = f.suggest_quantity(D('.05'), LOT)
        self.assertEqual(tiny.proposed, D(0))
        self.assertEqual(tiny.status, "REJECTED_RULE")
        self.assertEqual(tiny.reasons, ("BELOW_MINIMUM",))
        self.assertIn('BELOW_MINIMUM', f.grid_reasons(tiny.proposed, LOT))
        with self.assertRaises(ValueError):
            f.suggest_price(D(100), PRICE, rounding='AUTO_STOP')


class MarginTests(unittest.TestCase):
    def test_initial_maintenance_and_leverage(self):
        self.assertEqual(f.initial_margin(D(1000), 10).value, D(100))
        self.assertEqual(f.maintenance_margin(D(2000), TABLE).value, D(30))
        self.assertEqual(f.maintenance_margin(D(2000), TABLE).bracket_identity, TABLE.rows[1].identity)
        self.assertEqual(f.leverage_admissibility(D(999), 20, TABLE).status, 'VALID')
        self.assertEqual(f.leverage_admissibility(D(1000), 20, TABLE).status, 'VALID')
        self.assertEqual(f.select_bracket(D(1000), TABLE).bracket_identity, TABLE.rows[0].identity)
        self.assertEqual(f.select_bracket(D(10000), TABLE).status, 'VALID')

    def test_cap_inclusive_bracket_ownership_across_helpers(self):
        for notional, index in (('0', 0), ('999', 0), ('1000', 0),
                                ('1000.0001', 1), ('10000', 1)):
            with self.subTest(notional=notional):
                selected = f.select_bracket(D(notional), TABLE)
                self.assertEqual(selected.status, 'VALID')
                self.assertEqual(selected.bracket_identity, TABLE.rows[index].identity)
                maintenance = f.maintenance_margin(D(notional), TABLE)
                self.assertEqual(maintenance.bracket_identity, selected.bracket_identity)
                leverage = f.leverage_admissibility(D(notional), 20, TABLE)
                self.assertEqual(leverage.bracket_identity, selected.bracket_identity)
                self.assertEqual(leverage.status, 'VALID' if index == 0 else 'REJECTED_RULE')
        self.assertEqual(f.maintenance_margin(D(0), TABLE).value, D(0))
        self.assertEqual(f.maintenance_margin(D(1000), TABLE).value, D(10))
        self.assertEqual(f.maintenance_margin(D(10000), TABLE).value, D(190))
        for helper in (f.select_bracket, f.maintenance_margin):
            self.assertEqual(helper(D('10000.0001'), TABLE).status, 'UNAVAILABLE_BRACKETS')
        self.assertEqual(f.leverage_admissibility(D('10000.0001'), 1, TABLE).status, 'UNAVAILABLE_BRACKETS')

    def test_integer_leverage_domain_and_repeating_initial_margin(self):
        result = f.initial_margin(D(100), 3)
        self.assertEqual(result.status, 'VALID')
        self.assertEqual(result.value, ExactScalar(100, 3))
        self.assertIsNone(result.decimal_value)
        self.assertEqual(f.initial_margin(D(100), 8).decimal_value, D('12.5'))
        for invalid in (True, False, 0, -1, 126, 2.5, D('2.5'), D(2), '2'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    f.initial_margin(D(100), invalid)
                with self.assertRaises(ValueError):
                    f.leverage_admissibility(D(100), invalid, TABLE)
                with self.assertRaises(ValueError):
                    replace(TABLE.rows[0], max_initial_leverage=invalid)
        maximum = replace(TABLE, rows=(replace(TABLE.rows[0], max_initial_leverage=125), TABLE.rows[1]))
        self.assertEqual(f.leverage_admissibility(D(999), 125, maximum).status, 'VALID')
        self.assertEqual(f.leverage_admissibility(D(1000), 125, maximum).status, 'VALID')
        self.assertEqual(f.leverage_admissibility(D(100), 1, TABLE).status, 'VALID')
        with localcontext() as context:
            context.prec = 1
            self.assertEqual(result, f.initial_margin(D(100), 3))

    def test_bracket_malformed_tables(self):
        first, second = TABLE.rows
        for rows in ((replace(first, floor=D(1)), second), (first, replace(second, floor=D(1001))),
                     (first, replace(second, floor=D(999))), (second, first),
                     (first, replace(second, maintenance_rate=D(-1))),
                     (first, replace(second, cum=D(11))), (first, replace(second, cum=D(100))),
                     (first, replace(second, cap=D(1000))), (first, replace(second, max_initial_leverage=30)),
                     (first, replace(second, bracket_id='1'))):
            with self.subTest(rows=rows):
                self.assertEqual(f.validate_brackets(replace(TABLE, rows=rows)), ('INVALID_BRACKET_TABLE',))
        self.assertEqual(f.validate_brackets(None), ('UNAVAILABLE_BRACKETS',))
        self.assertEqual(f.validate_brackets(replace(TABLE, rows=())), ('UNAVAILABLE_BRACKETS',))

    def assert_root(self, side, q, entry, collateral, expected, bracket):
        result = f.liquidation_threshold(side, D(q), D(entry), D(collateral), TABLE)
        self.assertEqual(result.status, 'VALID')
        self.assertEqual(result.value, D(expected))
        self.assertEqual(result.bracket_identity, TABLE.rows[bracket].identity)
        equity = f.isolated_equity(side, D(q), D(entry), result.value, D(collateral)).value
        maintenance = f.maintenance_margin(f.notional(D(q), result.value).value, TABLE).value
        self.assertEqual(equity, maintenance)
        return result.value

    def test_long_short_cum_and_collateral(self):
        self.assert_root(LONG, '1', '1000', '109', '900', 0)
        self.assert_root(SHORT, '1', '1000', '112', '1100', 1)
        long = self.assert_root(LONG, '1', '2000', '128', '1900', 1)
        long_more = self.assert_root(LONG, '1', '2000', '226', '1800', 1)
        short = self.assert_root(SHORT, '1', '2000', '132', '2100', 1)
        short_more = self.assert_root(SHORT, '1', '2000', '234', '2200', 1)
        self.assertLess(long_more, long)
        self.assertGreater(short_more, short)

    def test_tier_crossing_re_solves_and_boundary(self):
        # Entry/current notional is tier 2, but long root belongs to tier 1.
        self.assert_root(LONG, '1', '2000', '1109', '900', 0)
        # Short entry notional is tier 1, root is tier 2.
        self.assert_root(SHORT, '1', '900', '212', '1100', 1)
        self.assert_root(LONG, '1', '1200', '210', '1000', 0)

    def test_liquidation_cap_boundary_belongs_to_preceding_tier(self):
        self.assert_root(LONG, '1', '1200', '210', '1000', 0)
        self.assert_root(SHORT, '1', '900', '110', '1000', 0)
        self.assert_root(LONG, '1', '10200', '390', '10000', 1)
        self.assert_root(SHORT, '1', '9900', '290', '10000', 1)
        # Once slightly above the first cap, only the second tier owns the root.
        self.assert_root(LONG, '1', '1200', '209.999902', '1000.0001', 1)
        # An exact supplied final cap remains usable even without another tier.
        incomplete = replace(TABLE, rows=TABLE.rows[:1])
        result = f.liquidation_threshold(SHORT, D(1), D(900), D(110), incomplete)
        self.assertEqual(result.status, 'VALID')
        self.assertEqual(result.value, D(1000))
        self.assertEqual(result.bracket_identity, incomplete.rows[0].identity)

    def test_no_position_missing_incomplete_no_positive(self):
        self.assertEqual(f.liquidation_threshold(LONG, D(0), D(100), D(10), None).status, 'NO_POSITION')
        self.assertEqual(f.liquidation_threshold(LONG, D(1), D(100), D(10), None).status, 'UNAVAILABLE_BRACKETS')
        self.assertEqual(f.liquidation_threshold(LONG, D(1), D(100), D(100), TABLE).status, 'NO_POSITIVE_THRESHOLD')
        incomplete = replace(TABLE, rows=TABLE.rows[:1])
        self.assertEqual(f.liquidation_threshold(SHORT, D(1), D(900), D(212), incomplete).status,
                         'UNAVAILABLE_BRACKETS')
        broken = replace(TABLE, rows=(TABLE.rows[0], replace(TABLE.rows[1], cum=D(9))))
        self.assertEqual(f.liquidation_threshold(LONG, D(1), D(100), D(10), broken).status, 'INVALID_BRACKET_TABLE')

    def test_repeating_root_is_valid_and_composable(self):
        result = f.liquidation_threshold(LONG, D(1), D(100), D(10), TABLE)
        self.assertEqual(result.status, 'VALID')
        self.assertEqual(result.value, ExactScalar(1000, 11))
        self.assertIsNone(result.decimal_value)
        equity = f.isolated_equity(LONG, D(1), D(100), result.value, D(10)).value
        mm = f.maintenance_margin(f.notional(D(1), result.value).value, TABLE).value
        self.assertEqual(equity, mm)
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(result, f.liquidation_threshold(LONG, D(1), D(100), D(10), TABLE))


class TriggerAndPolicyTests(unittest.TestCase):
    def test_all_directions_references_equalities_and_missing(self):
        for reference in PriceReference:
            for kind, side, greater in ((TriggerType.STOP, BUY, True), (TriggerType.STOP, SELL, False),
                                         (TriggerType.TAKE_PROFIT, BUY, False), (TriggerType.TAKE_PROFIT, SELL, True)):
                trigger = Trigger(side, kind, reference, D(100))
                key = 'contract_price' if reference is PriceReference.CONTRACT_PRICE else 'mark_price'
                for price in (D(99), D(100), D(101)):
                    other = {'mark_price': D(1)} if key == 'contract_price' else {'contract_price': D(1)}
                    self.assertEqual(f.trigger_predicate(trigger, **{key: price}, **other).value,
                                     price >= 100 if greater else price <= 100)
                self.assertEqual(f.trigger_predicate(trigger).status, 'UNAVAILABLE_TRIGGER_PRICE')
                self.assertEqual(f.trigger_predicate(replace(trigger, price_protect=True), **{key: D(100)}).status,
                                 'UNSUPPORTED_PRICE_PROTECTION')
        with self.assertRaises(ValueError):
            Trigger(BUY, 'TRAILING_STOP', PriceReference.MARK_PRICE, D(100))

    def test_fixed_bps(self):
        policy = FixedBpsPolicy(D(10), EVIDENCE)
        self.assertEqual(f.adverse_price(D(100), BUY, policy).value, D('100.1'))
        self.assertEqual(f.adverse_price(D(100), SELL, policy).value, D('99.9'))
        self.assertEqual(f.adverse_price(D(100), BUY, replace(policy, bps=D(0))).value, D(100))
        with self.assertRaises(ValueError):
            replace(policy, bps=D(-1))
        self.assertEqual(f.adverse_price(D(100), SELL, replace(policy, bps=D(10000))).status, 'REJECTED_RULE')

    def test_parameter_metadata_and_result_identity(self):
        self.assertNotEqual(FEES.identity, replace(FEES, maker_rate=D('.0003')).identity)
        self.assertNotEqual(PRICE.identity, replace(PRICE, tick_size=D('.5')).identity)
        self.assertNotEqual(TABLE.identity, replace(TABLE, rows=(TABLE.rows[0], replace(TABLE.rows[1], maintenance_rate=D('.03')))).identity)
        bps = FixedBpsPolicy(D(10), EVIDENCE)
        self.assertNotEqual(bps.identity, replace(bps, bps=D(11)).identity)
        self.assertNotEqual(f.suggest_price(D('100.1'), PRICE, rounding='FLOOR').identity,
                            f.suggest_price(D('100.1'), PRICE, rounding='CEIL').identity)
        self.assertNotEqual(TABLE.identity, replace(TABLE, rows=(TABLE.rows[0], replace(TABLE.rows[1], cum=D(11)))).identity)
        self.assertNotEqual(TABLE.identity, replace(TABLE, rows=(replace(TABLE.rows[0], max_initial_leverage=19), TABLE.rows[1])).identity)
        for field, value in (('version', 'v2'), ('source', 'another'), ('effective_at', '2020-01-01'),
                             ('observed_at', '2021-01-01'), ('classification', Provenance.CURRENT_RULE_ASSUMPTION)):
            self.assertNotEqual(FEES.identity, replace(FEES, evidence=replace(EVIDENCE, **{field: value})).identity)
        self.assertNotEqual(f.fee(D(1), D(100), FeeRole.MAKER, FEES).identity,
                            f.fee(D(1), D(100), FeeRole.TAKER, FEES).identity)
        trigger = Trigger(BUY, TriggerType.STOP, PriceReference.MARK_PRICE, D(100))
        self.assertNotEqual(trigger.identity, replace(trigger, reference=PriceReference.CONTRACT_PRICE).identity)
        self.assertEqual(f.fee(D(1), D(100), FeeRole.MAKER, FEES).algorithm_version,
                         ALGORITHM_VERSION)
        with self.assertRaises(ValueError):
            replace(SCOPE, margin_mode=[])
        with self.assertRaises(ValueError):
            replace(PRICE, tick_size=[])
        with self.assertRaises(FrozenInstanceError):
            FEES.maker_rate = D(1)
        with self.assertRaises(ValueError):
            replace(TABLE, rows=list(TABLE.rows))


if __name__ == '__main__':
    unittest.main()
