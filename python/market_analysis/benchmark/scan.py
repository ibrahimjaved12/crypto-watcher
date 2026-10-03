"""Stop-aware first-touch labeler for the #182 benchmark (bar mode). Integers and Fractions only.

Conventions
- Sides are +1 (long) and -1 (short). A decision is made at signal time ``s``,
  the end of minute ``d``. The entry minute is ``e = d + 1`` and the trade enters
  at the open of minute ``e`` plus slippage: the bar-mode version of "first trade
  at or after signal + 1 s" (tick mode is v2).
- ``p0 = open[e]``. Entry fill ``costs.entry_fill_price(side, open, high, low)``
  of minute ``e`` and a taker fee at that fill.
- Risk unit ``d = |p0 - stop|`` (a positive price distance on the losing side).
  Every result is per unit of base asset divided by ``d``: R multiples.
- Stop: triggers when a minute trades through it (long ``low <= stop``, short
  ``high >= stop``); fill ``costs.stop_fill_price`` (gap-aware: a gap through the
  stop fills from the worse open), taker fee.
- Target: a maker limit order, hit only when price trades strictly through it by
  one tick (long ``high >= target + tick``, short ``low <= target - tick``); fill at
  the target, maker fee, no slippage. Targets are NOT tested in the entry minute;
  the stop and liquidation are.
- Liquidation: ``L = costs.max_admissible_leverage(side, fill, |fill - stop|)``
  (None => non-trade 'N'), ``LP = costs.liquidation_price``. Long is liquidated
  when ``mark_low <= LP`` (compared as ``mark_low <= floor(LP)``), short when
  ``mark_high >= LP`` (``>= ceil(LP)``). Loss ``costs.liquidation_loss(...,
  leverage=L)``; net floored at ``-fill / L`` (the isolated wallet).
- Expiry after ``window_minutes``: market exit at the close of the last minute
  (``costs.entry_fill_price(-side, close, high, low)``), taker fee.
- Funding: an exit in minute ``x`` pays the settlements with
  ``open_time(e) < calc_time <= open_time(x)`` at ``funding.settlement_mark``
  (the mark open of the settlement minute) via ``costs.funding_cash``. A missing
  settlement mark makes the cell 'X'.
- Data quality: ``next_compromised(series)`` gives, per minute, the first minute
  at or after it with a ``COMPROMISED_FLAGS`` bit. A compromised entry minute is
  non-trade 'C'. Prices of compromised minutes are never read (missing trade or
  mark prices always carry such a flag); a trade with no event before the first
  compromised minute inside its window is 'X' at that minute.
- The window must fit in the series (``e + window - 1 < minutes``), else 'I'.

First event per target ``j`` (stop, liquidation and every target are found in
one forward pass):
- target at ``t_j`` wins if ``t_j < stop`` and ``t_j < liquidation``;
- else liquidation if ``liquidation <= stop`` (beats stop and target in its minute);
- else the stop; if ``t_j == stop`` it is a tie: a minute open at or through the
  stop means stop (gap), an open at or beyond ``target + tick`` (short: ``- tick``)
  means target, otherwise AMBIGUOUS (pessimistic cell = stop, optimistic = target);
- no event: 'X' at the first compromised minute in the window, else expiry 'E'.

Outcomes: T target, S stop, E expired, L liquidated, X data compromised.
Per cell (exact Fractions, then ``round(value / d * 10**6)`` half-even):
``net_ur`` (after fees, slippage and funding), ``fund_ur`` and ``gross_ur =
side * (exit_ref - p0)`` with ``exit_ref`` = target / ``min(stop, open_x)`` (short:
``max``) / ``close_x`` / ``LP``; ``cost_ur = gross_ur + fund_ur - net_ur``. So
``net(m) = gross + fund - m * cost`` holds exactly at ``m = 1``; the evaluator
uses it for the 0x/1x/2x/3x cost grid as a linear extrapolation from the m = 1
fills (the exact B1 cost model is used at m = 1). 'X' cells carry no amounts.
"""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor

from . import costs
from .bars import COMPROMISED_FLAGS, BarSeries
from .funding import FundingSeries, settlement_mark

UR = 10 ** 6
NON_TRADE_STATUSES = ("V", "C", "G", "N", "I")
OUTCOMES = ("T", "S", "E", "L", "X")


@dataclass(frozen=True)
class Cell:
    outcome: str
    exit_offset: int
    net_ur: int | None
    cost_ur: int | None
    fund_ur: int | None


@dataclass(frozen=True)
class CellPair:
    pess: Cell
    opt: Cell | None = None  # set only when the stop/target tie is ambiguous


@dataclass(frozen=True)
class TradeLabel:
    status: str
    p0: int | None = None
    leverage: int | None = None
    wallet_ur: int | None = None
    cells: tuple = ()


def next_compromised(series: BarSeries) -> array:
    """Per minute, the index of the first minute >= it with a compromised flag (``minutes`` if none)."""
    minutes = series.minutes
    result = array("q", [minutes]) * minutes
    following = minutes
    flags = series.flags
    for index in range(minutes - 1, -1, -1):
        if flags[index] & COMPROMISED_FLAGS:
            following = index
        result[index] = following
    return result


def label_trade(bars: BarSeries, funding: FundingSeries, model: costs.CostModel, *, tick: int, entry_index: int,
                side: int, stop_price: int, target_prices, window_minutes: int, next_comp=None) -> TradeLabel:
    if side not in (1, -1) or type(side) is not int:
        raise ValueError(f"side must be +1 or -1, got {side!r}")
    if type(tick) is not int or tick <= 0:
        raise ValueError(f"tick must be a positive int, got {tick!r}")
    if type(entry_index) is not int or entry_index < 0:
        raise ValueError(f"entry_index must be an int >= 0, got {entry_index!r}")
    if type(window_minutes) is not int or window_minutes < 1:
        raise ValueError(f"window_minutes must be an int >= 1, got {window_minutes!r}")
    targets = tuple(target_prices)
    if not targets or any(type(target) is not int for target in targets) or type(stop_price) is not int:
        raise ValueError("stop_price and at least one target price must be ints")
    e = entry_index
    last = e + window_minutes - 1
    if last >= bars.minutes:
        return TradeLabel("I")
    nc = next_compromised(bars) if next_comp is None else next_comp
    if nc[e] == e:
        return TradeLabel("C")
    opens, highs, lows, closes = bars.open, bars.high, bars.low, bars.close
    p0 = opens[e]
    if (side == 1 and not 0 < stop_price < p0) or (side == -1 and not stop_price > p0):
        raise ValueError(f"stop {stop_price} is not on the losing side of entry {p0} for side {side}")
    if any((target - p0) * side <= 0 for target in targets):
        raise ValueError(f"targets {targets} are not all on the winning side of entry {p0}")
    risk = abs(p0 - stop_price)
    fill_in = costs.entry_fill_price(side, p0, highs[e], lows[e], model)
    fee_in = costs.fee(fill_in, model.taker_rate, model)
    leverage = costs.max_admissible_leverage(side, fill_in, abs(fill_in - stop_price), model)
    if leverage is None:
        return TradeLabel("N")
    lp = costs.liquidation_price(side, fill_in, leverage, model.mmr)

    # One forward pass, integer comparisons only.
    first_comp = nc[e]
    scan_end = min(last, first_comp - 1)
    stop_at = liq_at = None
    hits = [None] * len(targets)
    remaining = len(targets)
    if side == 1:
        liq_level, through = floor(lp), [target + tick for target in targets]
        mark_side, trade_side = bars.mark_low, lows
        for i in range(e, scan_end + 1):
            if mark_side[i] <= liq_level:
                liq_at = i
            if trade_side[i] <= stop_price:
                stop_at = i
            if i > e:
                high = highs[i]
                for j, level in enumerate(through):
                    if hits[j] is None and high >= level:
                        hits[j] = i
                        remaining -= 1
            if stop_at is not None or liq_at is not None or not remaining:
                break
    else:
        liq_level, through = ceil(lp), [target - tick for target in targets]
        mark_side, trade_side = bars.mark_high, highs
        for i in range(e, scan_end + 1):
            if mark_side[i] >= liq_level:
                liq_at = i
            if trade_side[i] >= stop_price:
                stop_at = i
            if i > e:
                low = lows[i]
                for j, level in enumerate(through):
                    if hits[j] is None and low <= level:
                        hits[j] = i
                        remaining -= 1
            if stop_at is not None or liq_at is not None or not remaining:
                break

    cache: dict = {}

    def cell(outcome: str, x: int, target: int | None = None) -> Cell:
        key = (outcome, x, target)
        if key not in cache:
            cache[key] = _cell(bars, funding, model, side=side, e=e, x=x, outcome=outcome, target=target,
                               stop_price=stop_price, p0=p0, risk=risk, fill_in=fill_in, fee_in=fee_in,
                               leverage=leverage, lp=lp)
        return cache[key]

    never = scan_end + 1
    stop_i = never if stop_at is None else stop_at
    liq_i = never if liq_at is None else liq_at
    pairs = []
    for j, target in enumerate(targets):
        hit_i = never if hits[j] is None else hits[j]
        if hit_i < stop_i and hit_i < liq_i:
            pairs.append(CellPair(cell("T", hit_i, target)))
        elif liq_at is not None and liq_i <= stop_i:
            pairs.append(CellPair(cell("L", liq_i)))
        elif stop_at is not None:
            if hit_i != stop_i:
                pairs.append(CellPair(cell("S", stop_i)))
                continue
            opened = opens[stop_i]
            if (side == 1 and opened <= stop_price) or (side == -1 and opened >= stop_price):
                pairs.append(CellPair(cell("S", stop_i)))  # gap through the stop: unambiguous
            elif (side == 1 and opened >= target + tick) or (side == -1 and opened <= target - tick):
                pairs.append(CellPair(cell("T", stop_i, target)))  # opened through the target
            else:
                pairs.append(CellPair(cell("S", stop_i), cell("T", stop_i, target)))  # ambiguous
        elif first_comp <= last:
            pairs.append(CellPair(cell("X", first_comp)))
        else:
            pairs.append(CellPair(cell("E", last)))
    wallet_ur = round(fill_in / leverage / risk * UR)
    return TradeLabel("T", p0, leverage, wallet_ur, tuple(pairs))


def _cell(bars: BarSeries, funding: FundingSeries, model: costs.CostModel, *, side: int, e: int, x: int,
          outcome: str, target: int | None, stop_price: int, p0: int, risk: int, fill_in: Fraction,
          fee_in: Fraction, leverage: int, lp: Fraction) -> Cell:
    offset = x - e
    if outcome == "X":
        return Cell("X", offset, None, None, None)
    fund = Fraction(0)
    for calc_time, rate, _ in funding.events_between(bars.open_time(e), bars.open_time(x)):
        mark = settlement_mark(bars, calc_time)
        if mark is None:
            return Cell("X", offset, None, None, None)
        fund += costs.funding_cash(side, mark, rate, model)
    if outcome == "L":
        net = max(-costs.liquidation_loss(side, fill_in, lp, model, leverage=leverage) - fee_in + fund,
                  -fill_in / leverage)
        gross = side * (lp - p0)
    else:
        if outcome == "T":
            exit_fill = Fraction(target)
            fee_out = costs.fee(exit_fill, model.maker_rate, model)
            exit_ref = target
        elif outcome == "S":
            opened = bars.open[x]
            exit_fill = costs.stop_fill_price(side, stop_price, opened, bars.high[x], bars.low[x], model)
            fee_out = costs.fee(exit_fill, model.taker_rate, model)
            exit_ref = min(stop_price, opened) if side == 1 else max(stop_price, opened)
        elif outcome == "E":
            exit_fill = costs.entry_fill_price(-side, bars.close[x], bars.high[x], bars.low[x], model)
            fee_out = costs.fee(exit_fill, model.taker_rate, model)
            exit_ref = bars.close[x]
        else:
            raise ValueError(f"unknown outcome {outcome!r}")
        net = side * (exit_fill - fill_in) - fee_in - fee_out + fund
        gross = side * (exit_ref - p0)
    net_ur = round(net * UR / risk)
    fund_ur = round(fund * UR / risk)
    gross_ur = round(Fraction(gross) * UR / risk)
    return Cell(outcome, offset, net_ur, gross_ur + fund_ur - net_ur, fund_ur)
