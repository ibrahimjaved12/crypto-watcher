"""Bid/ask resolver for forex setups (#374), mirroring ``forward.outcomes.resolve_setup``.

Triggers use BID candles plus the minute's spread (``fx.costs.stop_hit`` / ``target_hit``). A gap
through the stop fills from the worse open; a target fills at the target. Both in one minute is
``ambiguous``: the pessimistic (stop) result is reported and the target result is kept only in
``optimistic``, unless the minute OPENS beyond a barrier (bid open for a long exit, ask open for a
short exit): that barrier was reached first. A minute missing while open (including the entry
minute) ends the trade as ``X`` (no trustworthy price). The window
lies in one open session by construction, so a closed minute inside it is an error, not an exit.

Money is exact and in R (1 R = |entry fill - stop|), rounded half-even once per reported number:
slippage and round-trip commission are costs; swap at each rollover is the funding term. The spread
is already inside ``p0`` (reported as ``spread_entry_ur`` for information only). Stop-out sizing is
the wallet's job, so ``label_leverage`` and ``wallet_ur`` stay ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

from ..benchmark.canonical import exact_from_str, exact_to_str
from ..forward.outcomes import Resolution
from ..trading_calendar import TradingCalendar
from .bars import FLAG_MISSING_OPEN, FLAG_PRESENT, FxBarSeries
from .costs import (FxContract, FxCostModel, SwapRates, entry_fill_price, entry_reference, exit_price_at_market,
                    market_exit_fill,
                    stop_fill_price, stop_hit, target_fill_price, target_hit)
from .setups import FxSetup

_SCALE = 10 ** 8
_UR = 10 ** 6


@dataclass(frozen=True)
class FxResolution(Resolution):
    entry_fill: Optional[str] = None
    exit_fill: Optional[str] = None
    slippage_ur: Optional[int] = None
    commission_ur: Optional[int] = None
    spread_entry_ur: Optional[int] = None  # informational: spread0 / R, the cost already inside p0


def _money(setup: FxSetup, entry_fill: Fraction, exit_ref: Fraction, exit_fill: Fraction, exit_ms: int,
           calendar: TradingCalendar, model: FxCostModel, contract: FxContract, rates: Optional[SwapRates],
           quote_to_account: Fraction) -> dict:
    side = setup.side
    risk = abs(entry_fill - setup.stop)
    slip = side * (entry_fill - setup.p0) + side * (exit_ref - exit_fill)
    commission = (2 * model.commission_per_lot_side * model.cost_multiplier / contract.contract_size
                  / quote_to_account * _SCALE)
    swap = Fraction(0)
    if rates is not None:
        per_unit = rates.long_per_unit if side == 1 else rates.short_per_unit
        for _instant, multiplier in calendar.rollover_events(setup.entry_ms, exit_ms):
            swap += multiplier * model.swap_multiplier * per_unit * _SCALE
    net = (side * (exit_fill - entry_fill) - commission + swap) / risk
    return {"net_ur": round(net * _UR), "cost_ur": round((slip + commission) / risk * _UR),
            "fund_ur": round(swap / risk * _UR), "slippage_ur": round(slip / risk * _UR),
            "commission_ur": round(commission / risk * _UR), "spread_entry_ur": round(setup.spread0 / risk * _UR)}


def resolve_fx_setup(setup: FxSetup, bars: FxBarSeries, calendar: TradingCalendar, model: FxCostModel,
                     contract: FxContract, *, rates: Optional[SwapRates] = None,
                     quote_to_account: Fraction = Fraction(1), previous: Optional[Resolution] = None) -> FxResolution:
    if previous is not None and previous.final:
        return previous
    if setup.status != "T":
        raise ValueError(f"setup {setup.setup_id} is not tradeable (status {setup.status})")
    if bars.symbol != setup.symbol:
        raise ValueError("bars belong to another symbol")
    if type(quote_to_account) not in (int, Fraction) or quote_to_account <= 0:
        raise ValueError("quote_to_account must be a positive int or Fraction")
    if setup.entry_ms >= bars.end_ms:
        return FxResolution(setup.setup_id, "pending")
    side, stop, target = setup.side, setup.stop, setup.target
    e = bars.index_of(setup.entry_ms)
    if bars.flags[e] == FLAG_MISSING_OPEN:  # no trustworthy entry price: never invent a fill
        return FxResolution(setup.setup_id, "X", resolved_through_ms=setup.entry_ms, exit_offset=0,
                            exit_ms=setup.entry_ms)
    if bars.flags[e] != FLAG_PRESENT:
        raise ValueError("window crosses closed market")
    p0 = entry_reference(side, bars.open[e], bars.spread[e])
    if p0 != setup.p0:
        raise ValueError("entry open/spread differ from the ones the setup was built on")
    entry_fill = entry_fill_price(side, p0, bars.spread[e], bars.high[e], bars.low[e], model)
    if entry_fill != exact_from_str(setup.entry_fill):
        raise ValueError("entry minute differs from the one the setup was built on")
    available = min(setup.window_minutes, bars.minutes - e)

    def done(status, j, exit_ref, exit_fill, optimistic=None):
        i = e + j
        exit_ms = bars.open_time(i)
        money = _money(setup, entry_fill, exit_ref, exit_fill, exit_ms, calendar, model, contract, rates,
                       Fraction(quote_to_account))
        return FxResolution(setup.setup_id, status, resolved_through_ms=exit_ms, exit_offset=j, exit_ms=exit_ms,
                            exit_ref_price=round(exit_ref), optimistic=optimistic, entry_fill=exact_to_str(entry_fill),
                            exit_fill=exact_to_str(exit_fill), **money)

    def stop_exit(i):
        ref = min(Fraction(stop), Fraction(bars.open[i])) if side == 1 else \
            max(Fraction(stop), Fraction(bars.open[i] + bars.spread[i]))
        fill = stop_fill_price(side, stop, bars.open[i], bars.spread[i], bars.high[i], bars.low[i], model)
        return ref, fill

    for j in range(available):
        i = e + j
        flag = bars.flags[i]
        if flag == FLAG_MISSING_OPEN:
            ms = bars.open_time(i)
            return FxResolution(setup.setup_id, "X", resolved_through_ms=ms, exit_offset=j, exit_ms=ms,
                                entry_fill=exact_to_str(entry_fill))
        if flag != FLAG_PRESENT:
            raise ValueError("window crosses closed market")
        spread, high, low = bars.spread[i], bars.high[i], bars.low[i]
        if j > 0:  # the open is the first executable price of a later minute: a gap decides the order
            exit_open = bars.open[i] if side == 1 else bars.open[i] + spread  # long sells at bid, short buys at ask
            if side * (exit_open - target) >= 0:
                return done("T", j, Fraction(target), target_fill_price(target))
            if side * (exit_open - stop) <= 0:
                return done("S", j, *stop_exit(i))
        hit_stop = stop_hit(side, stop, spread, high, low)
        hit_target = target_hit(side, target, spread, high, low)
        if hit_stop and hit_target:
            target_result = done("T", j, Fraction(target), target_fill_price(target))
            optimistic = {"outcome": "T", "net_ur": target_result.net_ur, "cost_ur": target_result.cost_ur,
                          "fund_ur": target_result.fund_ur}
            return done("ambiguous", j, *stop_exit(i), optimistic=optimistic)
        if hit_stop:
            return done("S", j, *stop_exit(i))
        if hit_target:
            return done("T", j, Fraction(target), target_fill_price(target))
    if available == setup.window_minutes:
        i = e + available - 1
        ref = exit_price_at_market(side, bars.close[i], bars.spread[i])
        fill = market_exit_fill(side, bars.close[i], bars.spread[i], bars.high[i], bars.low[i], model)
        return done("E", available - 1, ref, fill)
    return FxResolution(setup.setup_id, "open", resolved_through_ms=bars.open_time(e + available - 1),
                        entry_fill=exact_to_str(entry_fill))


__all__ = ["FxResolution", "resolve_fx_setup"]
