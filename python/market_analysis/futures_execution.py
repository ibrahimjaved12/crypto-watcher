"""Stateless Binance USD-M math. No I/O, account admission or settlement.

Arithmetic uses reduced exact rational scalars, independent of Decimal context.
Finite results expose exact Decimal values; repeating results remain valid,
composable ExactScalar values without introducing a rounding assumption.
"""

from decimal import Decimal

from .canonical_identity import canonical_digest
from .exact_scalar import exact_scalar, finite_or_exact

from .futures_execution_contracts import (
    Adjustment, BracketTable, ContractRules, Evidence, FeePolicy, FeeRole,
    FixedBpsPolicy, Grid, IncreaseCalculation, OrderIntent, OrderSide, PositionCalculation,
    PriceFilter, RuleEvaluationContext,
    PositionSide, PriceReference, Provenance, Result, Scope, Trigger,
    TriggerType, enum_value, leverage_value, number,
)


ONE = Decimal(1)


def _add(a, b):
    return finite_or_exact(exact_scalar(a) + exact_scalar(b))


def _sub(a, b):
    return finite_or_exact(exact_scalar(a) - exact_scalar(b))


def _mul(a, b):
    return finite_or_exact(exact_scalar(a) * exact_scalar(b))


def _divide(a, b):
    return finite_or_exact(exact_scalar(a) / exact_scalar(b))


def _result(calculation, inputs, value=None, *, status="VALID", reasons=(), bracket=None):
    return Result(status, reasons, calculation, canonical_digest(inputs), value,
                  bracket.identity if bracket is not None else None)


def _quotient(calculation, inputs, numerator, denominator, bracket=None):
    return _result(calculation, inputs, _divide(numerator, denominator), bracket=bracket)


def _sign(side):
    enum_value(side, PositionSide)
    return ONE if side is PositionSide.LONG else Decimal(-1)


def _evidence_available(evidence):
    return isinstance(evidence, Evidence) and evidence.classification is not Provenance.UNAVAILABLE


def scope_reasons(scope):
    if not isinstance(scope, Scope):
        return ("UNSUPPORTED_SCOPE",)
    reasons = []
    symbol = scope.instrument_id.removeprefix("binance-usdm:")
    if (not scope.instrument_id.startswith("binance-usdm:")
            or not symbol.endswith("USDT") or len(symbol) <= 4
            or not symbol.isascii() or not symbol.isalnum() or symbol != symbol.upper()):
        reasons.append("UNSUPPORTED_INSTRUMENT")
    for name, expected in (("settlement", "USDT"), ("position_mode", "ONE_WAY"),
                           ("margin_mode", "ISOLATED"), ("multi_assets", False),
                           ("portfolio_margin", False), ("bnb_fee_state", False),
                           ("real_trading", False), ("authenticated", False)):
        if getattr(scope, name) != expected:
            reasons.append("UNSUPPORTED_" + name.upper())
    return tuple(reasons)


def notional(quantity, price):
    number(quantity, "quantity", minimum=0)
    number(price, "price", positive=True)
    return _result("notional", (quantity, price), _mul(quantity, price))


def unrealized_pnl(side, quantity, entry, mark):
    s = _sign(side)
    number(quantity, "quantity", minimum=0)
    number(entry, "entry", positive=True)
    number(mark, "mark", positive=True)
    return _result("unrealized_pnl", (side, quantity, entry, mark),
                   _mul(s, _mul(quantity, _sub(mark, entry))))


def increase(quantity, entry, added_quantity, fill):
    for name, value in (("quantity", quantity), ("entry", entry),
                        ("added_quantity", added_quantity), ("fill", fill)):
        number(value, name, positive=True)
    total = _add(quantity, added_quantity)
    average = _quotient("increase_entry", (quantity, entry, added_quantity, fill),
                        _add(_mul(quantity, entry), _mul(added_quantity, fill)), total)
    return IncreaseCalculation(total, average)


def reduce(side, quantity, entry, closed_quantity, fill):
    _sign(side)
    for name, value in (("quantity", quantity), ("entry", entry),
                        ("closed_quantity", closed_quantity), ("fill", fill)):
        number(value, name, positive=True)
    if closed_quantity > quantity:
        raise ValueError("reduction exceeds position; split reversal explicitly")
    remaining = _sub(quantity, closed_quantity)
    pnl = unrealized_pnl(side, closed_quantity, entry, fill).value
    return PositionCalculation(remaining, entry if remaining else None, pnl,
                               canonical_digest((side, quantity, entry, closed_quantity, fill)))


def fee(quantity, fill, role, policy):
    number(quantity, "fill_quantity", positive=True)
    number(fill, "fill_price", positive=True)
    enum_value(role, FeeRole)
    if not isinstance(policy, FeePolicy):
        raise ValueError("fee policy required")
    inputs = (quantity, fill, role, policy)
    if not _evidence_available(policy.evidence):
        return _result("fee", inputs, status="UNAVAILABLE_RULE", reasons=("UNAVAILABLE_FEE_POLICY",))
    rate = policy.maker_rate if role is FeeRole.MAKER else policy.taker_rate
    return _result("fee", inputs, _mul(_mul(quantity, fill), rate))


def funding(side, eligible_quantity, settled_rate, settlement_mark):
    s = _sign(side)
    number(eligible_quantity, "eligible_quantity", minimum=0)
    number(settled_rate, "settled_rate")
    inputs = (side, eligible_quantity, settled_rate, settlement_mark)
    if settlement_mark is None:
        return _result("funding", inputs, status="UNAVAILABLE_FUNDING_MARK",
                       reasons=("UNAVAILABLE_FUNDING_MARK",))
    number(settlement_mark, "settlement_mark", positive=True)
    return _result("funding", inputs,
                   _mul(_mul(s.copy_negate(), eligible_quantity), _mul(settlement_mark, settled_rate)))


def _grid_errors(grid):
    if not isinstance(grid, Grid):
        return ("UNAVAILABLE_GRID",)
    try:
        number(grid.minimum, "minimum", minimum=0)
        number(grid.maximum, "maximum", positive=True)
        number(grid.increment, "increment", positive=True)
    except ValueError:
        return ("INVALID_GRID",)
    if grid.maximum < grid.minimum:
        return ("INVALID_GRID_BOUNDS",)
    if not _evidence_available(grid.evidence):
        return ("UNAVAILABLE_GRID",)
    return ()


def _grid_ratio(value, grid):
    delta = _sub(value, grid.minimum)
    n, d = delta.as_integer_ratio()
    sn, sd = grid.increment.as_integer_ratio()
    return n * sd, d * sn


def grid_reasons(value, grid):
    number(value, "grid value", minimum=0)
    errors = _grid_errors(grid)
    if errors:
        return errors
    reasons = []
    if value < grid.minimum:
        reasons.append("BELOW_MINIMUM")
    if value > grid.maximum:
        reasons.append("ABOVE_MAXIMUM")
    n, d = _grid_ratio(value, grid)
    if n % d:
        reasons.append("OFF_GRID")
    return tuple(reasons)


def suggest_grid(value, grid, *, rounding):
    """Explicit quantity proposal on the minimum-based Binance lattice."""
    number(value, "value", minimum=0)
    if _grid_errors(grid):
        raise ValueError("unavailable/invalid grid")
    if rounding not in ("FLOOR", "CEIL"):
        raise ValueError("explicit FLOOR or CEIL required")
    n, d = _grid_ratio(value, grid)
    steps = n // d if rounding == "FLOOR" else -(-n // d)
    proposed = _add(grid.minimum, _mul(Decimal(steps), grid.increment))
    errors = grid_reasons(proposed, grid) if proposed >= 0 else ("BELOW_MINIMUM",)
    return Adjustment(value, proposed, rounding, grid.identity,
                      status="REJECTED_RULE" if errors else "SUGGESTED", reasons=errors)


def suggest_quantity(quantity, grid):
    return suggest_grid(quantity, grid, rounding="FLOOR")


def _price_filter_errors(rule):
    if not isinstance(rule, PriceFilter):
        return ("UNAVAILABLE_PRICE_FILTER",)
    try:
        for name in ("min_price", "max_price", "tick_size"):
            number(getattr(rule, name), name, minimum=0)
    except ValueError:
        return ("INVALID_PRICE_FILTER",)
    if rule.max_price and rule.min_price > rule.max_price:
        return ("INVALID_PRICE_FILTER_BOUNDS",)
    if not _evidence_available(rule.evidence):
        return ("UNAVAILABLE_PRICE_FILTER",)
    return ()


def price_filter_reasons(price, rule):
    number(price, "price", minimum=0)
    errors = _price_filter_errors(rule)
    if errors:
        return errors
    reasons = []
    if rule.min_price and price < rule.min_price:
        reasons.append("BELOW_MINIMUM")
    if rule.max_price and price > rule.max_price:
        reasons.append("ABOVE_MAXIMUM")
    if rule.tick_size:
        n, d = _divide(_sub(price, rule.min_price), rule.tick_size).as_integer_ratio()
        if n % d:
            reasons.append("OFF_GRID")
    return tuple(reasons)


def suggest_price(price, rule, *, rounding):
    """Explicit price/stop/target proposal; no tick means no invented adjustment."""
    number(price, "price", minimum=0)
    if _price_filter_errors(rule):
        raise ValueError("unavailable/invalid price filter")
    if rounding not in ("FLOOR", "CEIL"):
        raise ValueError("explicit FLOOR or CEIL required")
    if rule.tick_size == 0:
        errors = price_filter_reasons(price, rule)
        return Adjustment(price, price, "NO_ENABLED_TICK", rule.identity,
                          status="REJECTED_RULE" if errors else "UNCHANGED",
                          reasons=errors or ("PRICE_TICK_DISABLED",))
    n, d = _divide(_sub(price, rule.min_price), rule.tick_size).as_integer_ratio()
    steps = n // d if rounding == "FLOOR" else -(-n // d)
    proposed = _add(rule.min_price, _mul(Decimal(steps), rule.tick_size))
    errors = price_filter_reasons(proposed, rule) if proposed >= 0 else ("BELOW_MINIMUM",)
    return Adjustment(price, proposed, rounding, rule.identity,
                      status="REJECTED_RULE" if errors else "SUGGESTED", reasons=errors)


def suggest_passive_limit(price, side, rule):
    enum_value(side, OrderSide)
    return suggest_price(price, rule, rounding="FLOOR" if side is OrderSide.BUY else "CEIL")


def validate_order(intent, rules, context=None):
    if not isinstance(intent, OrderIntent) or not isinstance(rules, ContractRules):
        raise ValueError("explicit intent and contract rules required")
    if context is not None and not isinstance(context, RuleEvaluationContext):
        raise ValueError("explicit RuleEvaluationContext or None required")
    inputs = (intent, rules, context)
    mark = (context.mark_price if context is not None
            and _evidence_available(context.evidence) else None)
    unsupported = scope_reasons(rules.scope)
    if unsupported:
        return _result("validate_order", inputs, status="UNSUPPORTED_SCOPE", reasons=unsupported)
    reasons = []
    unavailable = False
    invalid = False

    def add(code, *, missing=False, malformed=False):
        nonlocal unavailable, invalid
        if code not in reasons:
            reasons.append(code)
        unavailable |= missing
        invalid |= malformed

    if not _evidence_available(rules.evidence):
        add("UNAVAILABLE_RULE_PROVENANCE", missing=True)
    for name, value, expected in (("STATUS", rules.status, "TRADING"),
                                  ("CONTRACT_TYPE", rules.contract_type, "PERPETUAL")):
        if value is None:
            add("UNAVAILABLE_" + name, missing=True)
        elif value != expected:
            add("UNSUPPORTED_" + name)
    for name in ("percent_price_required",):
        if type(getattr(rules, name)) is not bool:
            add("INVALID_" + name.upper(), malformed=True)
    if rules.trigger_protect is not None:
        try:
            number(rules.trigger_protect, "trigger_protect", minimum=0)
        except ValueError:
            add("INVALID_TRIGGER_PROTECT", malformed=True)

    # Filter applicability follows the immutable order form.
    name, grid = (("MARKET_LOT_SIZE", rules.market_lot) if intent.market
                  else ("LOT_SIZE", rules.lot))
    errors = _grid_errors(grid)
    if not errors:
        errors = grid_reasons(intent.quantity, grid)
    for error in errors:
        add(name + ":" + error, missing=error.startswith("UNAVAILABLE"),
            malformed=error.startswith("INVALID"))
    if not intent.market:
        price_errors = _price_filter_errors(rules.price)
        if not price_errors:
            price_errors = price_filter_reasons(intent.price, rules.price)
        for error in price_errors:
            add("PRICE_FILTER:" + error, missing=error.startswith("UNAVAILABLE"),
                malformed=error.startswith("INVALID"))
    reference = mark if intent.market else intent.price
    minimum = rules.min_notional
    if minimum is None or not _evidence_available(minimum.evidence):
        add("UNAVAILABLE_MIN_NOTIONAL", missing=True)
    else:
        try:
            number(minimum.minimum, "minimum notional", minimum=0)
            if type(minimum.reduce_only_exempt) is not bool:
                raise ValueError("invalid exemption")
        except ValueError:
            add("INVALID_MIN_NOTIONAL", malformed=True)
        else:
            if not (intent.reduce_only and minimum.reduce_only_exempt):
                if reference is None:
                    add("UNAVAILABLE_MARK_PRICE", missing=True)
                elif _mul(intent.quantity, reference) < minimum.minimum:
                    add("BELOW_MIN_NOTIONAL")
    if not intent.market:
        percent = rules.percent_price
        if percent is None:
            if rules.percent_price_required:
                add("UNAVAILABLE_PERCENT_PRICE", missing=True)
        else:
            try:
                number(percent.multiplier_down, "percent down", positive=True)
                number(percent.multiplier_up, "percent up", positive=True)
                if percent.multiplier_down > percent.multiplier_up:
                    raise ValueError("invalid percent bounds")
            except ValueError:
                add("INVALID_PERCENT_PRICE", malformed=True)
            else:
                if not _evidence_available(percent.evidence):
                    add("UNAVAILABLE_PERCENT_PRICE", missing=True)
                elif intent.price is not None:
                    if mark is None:
                        add("UNAVAILABLE_MARK_PRICE", missing=True)
                    elif ((intent.side is OrderSide.BUY and intent.price > _mul(mark, percent.multiplier_up))
                          or (intent.side is OrderSide.SELL and intent.price < _mul(mark, percent.multiplier_down))):
                        add("OUTSIDE_PERCENT_PRICE")
    if invalid:
        status = "REJECTED_RULE"
    elif unavailable:
        status = "UNAVAILABLE_RULE"
    else:
        status = "REJECTED_RULE" if reasons else "VALID"
    return _result("validate_order", inputs, status=status, reasons=tuple(reasons))


def validate_brackets(table):
    if table is None:
        return ("UNAVAILABLE_BRACKETS",)
    if not isinstance(table, BracketTable):
        raise ValueError("bracket table required")
    if scope_reasons(table.scope):
        return ("UNSUPPORTED_SCOPE",)
    if not table.rows or not _evidence_available(table.evidence):
        return ("UNAVAILABLE_BRACKETS",)
    previous = None
    ids = set()
    for row in table.rows:
        try:
            number(row.floor, "floor", minimum=0)
            number(row.cap, "cap", positive=True)
            leverage_value(row.max_initial_leverage)
            number(row.maintenance_rate, "maintenance rate", minimum=0)
            number(row.cum, "cum", minimum=0)
        except ValueError:
            return ("INVALID_BRACKET_TABLE",)
        if (not isinstance(row.bracket_id, str) or not row.bracket_id or row.bracket_id in ids
                or row.cap <= row.floor or row.maintenance_rate >= 1
                or _sub(_mul(row.floor, row.maintenance_rate), row.cum) < 0):
            return ("INVALID_BRACKET_TABLE",)
        if previous is None:
            if row.floor != 0 or row.cum != 0:
                return ("INVALID_BRACKET_TABLE",)
        elif (row.floor != previous.cap
              or row.maintenance_rate < previous.maintenance_rate
              or row.max_initial_leverage > previous.max_initial_leverage
              or _sub(_mul(row.floor, row.maintenance_rate), row.cum)
              != _sub(_mul(row.floor, previous.maintenance_rate), previous.cum)):
            return ("INVALID_BRACKET_TABLE",)
        ids.add(row.bracket_id)
        previous = row
    return ()


def _bracket_contains(notional_value, row, *, first):
    # Positive cap belongs only to its preceding tier. Zero is assigned to the
    # first tier for zero-notional pure helpers (maintenance margin = 0).
    above_floor = notional_value >= row.floor if first else notional_value > row.floor
    return above_floor and notional_value <= row.cap


def select_bracket(notional_value, table):
    number(notional_value, "notional", minimum=0)
    inputs = (notional_value, table)
    errors = validate_brackets(table)
    if errors:
        return _result("select_bracket", inputs, status=errors[0], reasons=errors)
    # First tier includes zero; subsequent tiers are (floor, cap].
    for index, row in enumerate(table.rows):
        if _bracket_contains(notional_value, row, first=index == 0):
            return _result("select_bracket", inputs, bracket=row)
    return _result("select_bracket", inputs, status="UNAVAILABLE_BRACKETS",
                   reasons=("NOTIONAL_OUTSIDE_SUPPLIED_BRACKETS",))


def _selected_row(result, table):
    return next(r for r in table.rows if r.identity == result.bracket_identity)


def leverage_admissibility(notional_value, leverage, table):
    leverage_value(leverage)
    selected = select_bracket(notional_value, table)
    if selected.status != "VALID":
        return _result("leverage_admissibility", (notional_value, leverage, table),
                       status=selected.status, reasons=selected.reasons)
    row = _selected_row(selected, table)
    valid = leverage <= row.max_initial_leverage
    return _result("leverage_admissibility", (notional_value, leverage, table), valid,
                   status="VALID" if valid else "REJECTED_RULE",
                   reasons=() if valid else ("LEVERAGE_EXCEEDS_BRACKET",), bracket=row)


def initial_margin(notional_value, leverage):
    number(notional_value, "notional", minimum=0)
    leverage_value(leverage)
    return _quotient("initial_margin", (notional_value, leverage), notional_value, Decimal(leverage))


def maintenance_margin(notional_value, table):
    selected = select_bracket(notional_value, table)
    if selected.status != "VALID":
        return _result("maintenance_margin", (notional_value, table),
                       status=selected.status, reasons=selected.reasons)
    row = _selected_row(selected, table)
    return _result("maintenance_margin", (notional_value, table),
                   _sub(_mul(notional_value, row.maintenance_rate), row.cum), bracket=row)


def isolated_equity(side, quantity, entry, mark, isolated_wallet_collateral):
    number(isolated_wallet_collateral, "isolated wallet collateral", minimum=0)
    pnl = unrealized_pnl(side, quantity, entry, mark).value
    return _result("isolated_equity", (side, quantity, entry, mark, isolated_wallet_collateral),
                   _add(isolated_wallet_collateral, pnl))


def liquidation_threshold(side, quantity, entry, isolated_wallet_collateral, table):
    s = _sign(side)
    number(quantity, "quantity", minimum=0)
    number(entry, "entry", positive=True)
    number(isolated_wallet_collateral, "isolated wallet collateral", minimum=0)
    inputs = (side, quantity, entry, isolated_wallet_collateral, table)
    if quantity == 0:
        return _result("liquidation_threshold", inputs, status="NO_POSITION", reasons=("NO_POSITION",))
    errors = validate_brackets(table)
    if errors:
        return _result("liquidation_threshold", inputs, status=errors[0], reasons=errors)
    candidates = []
    positive_outside = False
    for index, row in enumerate(table.rows):
        numerator = _sub(_sub(_mul(s, _mul(quantity, entry)), isolated_wallet_collateral), row.cum)
        denominator = _mul(quantity, _sub(s, row.maintenance_rate))
        if denominator < 0:
            numerator, denominator = numerator.copy_negate(), denominator.copy_negate()
        if numerator <= 0:
            continue
        root = _divide(numerator, denominator)
        implied_notional = _mul(quantity, root)
        if implied_notional > table.rows[-1].cap:
            positive_outside = True
        if not _bracket_contains(implied_notional, row, first=index == 0):
            continue
        equity = isolated_equity(side, quantity, entry, root, isolated_wallet_collateral).value
        maintenance = _sub(_mul(_mul(quantity, root), row.maintenance_rate), row.cum)
        if equity != maintenance:
            return _result("liquidation_threshold", inputs, status="INVALID_BRACKET_TABLE",
                           reasons=("LIQUIDATION_EQUALITY_FAILED",))
        candidates.append((row, numerator, denominator))
    if len(candidates) > 1:
        return _result("liquidation_threshold", inputs, status="AMBIGUOUS_BRACKET_RESULT",
                       reasons=("AMBIGUOUS_BRACKET_RESULT",))
    if not candidates:
        status = "UNAVAILABLE_BRACKETS" if positive_outside else "NO_POSITIVE_THRESHOLD"
        return _result("liquidation_threshold", inputs, status=status, reasons=(status,))
    row, numerator, denominator = candidates[0]
    return _quotient("liquidation_threshold", inputs, numerator, denominator, row)


def trigger_predicate(trigger, *, contract_price=None, mark_price=None):
    if not isinstance(trigger, Trigger):
        raise ValueError("explicit trigger contract required; trailing stops unsupported")
    inputs = (trigger, contract_price, mark_price)
    for value in (contract_price, mark_price):
        if value is not None:
            number(value, "working price", positive=True)
    if trigger.price_protect:
        return _result("trigger", inputs, status="UNSUPPORTED_PRICE_PROTECTION",
                       reasons=("UNSUPPORTED_PRICE_PROTECTION",))
    value = contract_price if trigger.reference is PriceReference.CONTRACT_PRICE else mark_price
    if value is None:
        return _result("trigger", inputs, status="UNAVAILABLE_TRIGGER_PRICE",
                       reasons=("UNAVAILABLE_" + trigger.reference.value,))
    greater = ((trigger.kind is TriggerType.STOP and trigger.side is OrderSide.BUY)
               or (trigger.kind is TriggerType.TAKE_PROFIT and trigger.side is OrderSide.SELL))
    return _result("trigger", inputs, value >= trigger.price if greater else value <= trigger.price)


def adverse_price(reference_price, side, policy):
    number(reference_price, "reference price", positive=True)
    enum_value(side, OrderSide)
    if not isinstance(policy, FixedBpsPolicy):
        raise ValueError("versioned fixed-bps policy required")
    inputs = (reference_price, side, policy)
    if not _evidence_available(policy.evidence):
        return _result("adverse_price", inputs, status="UNAVAILABLE_RULE", reasons=("UNAVAILABLE_BPS_POLICY",))
    sign = ONE if side is OrderSide.BUY else Decimal(-1)
    factor = _add(ONE, _mul(sign, _divide(policy.bps, Decimal(10000))))
    if factor <= 0:
        return _result("adverse_price", inputs, status="REJECTED_RULE", reasons=("NON_POSITIVE_ADJUSTED_PRICE",))
    return _result("adverse_price", inputs, _mul(reference_price, factor))
