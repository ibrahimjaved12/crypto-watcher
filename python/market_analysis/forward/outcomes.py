"""Incremental resolution of forward setups with ``scan.label_trade`` (identical to ``build_labels``).

``resolve_setup(setup, bars, funding=None, previous=None)``:
- pending: the entry minute is not in ``bars`` yet;
- open: entered, no event yet and the time limit not reached;
- T / S / E / L / X: resolved exactly as the label cell (first touch, stop-aware, costs);
- ambiguous: the stop and the target are touched in the same minute without a gap: the
  pessimistic (stop) result is the net, the optimistic one is kept beside it, never chosen.
The scan runs on ``min(time limit, available minutes)``: an event inside the available minutes is
the same event the full window gives (the scan is a forward pass), so a resolution never changes
once final, and ``previous`` (a final resolution) is returned unchanged. Net results are micro-R
(1 R = the entry-to-stop distance), after fees, slippage and funding (``costs``).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..benchmark import costs
from ..benchmark.bars import BarSeries
from ..benchmark.funding import FundingSeries
from ..benchmark.scan import label_trade
from .bars_adapter import MINUTE_MS
from .setups import FORWARD_PARAMS, Setup

FINAL = ("T", "S", "E", "L", "X", "ambiguous")


@dataclass(frozen=True)
class Resolution:
    setup_id: str
    status: str
    resolved_through_ms: int | None = None
    exit_offset: int | None = None
    exit_ms: int | None = None
    exit_ref_price: int | None = None
    net_ur: int | None = None
    cost_ur: int | None = None
    fund_ur: int | None = None
    optimistic: dict | None = None
    label_leverage: int | None = None
    wallet_ur: int | None = None

    @property
    def final(self) -> bool:
        return self.status in FINAL

    def to_dict(self) -> dict:
        return asdict(self)


def empty_funding(bars: BarSeries) -> FundingSeries:
    return FundingSeries((), (), (), bars.start_ms, bars.end_ms)


def resolve_setup(setup: Setup, bars: BarSeries, funding: FundingSeries | None = None,
                  previous: Resolution | None = None) -> Resolution:
    if previous is not None and previous.final:
        return previous
    if setup.status != "T":
        raise ValueError(f"setup {setup.setup_id} is not tradeable (status {setup.status})")
    if bars.symbol != setup.symbol:
        raise ValueError("bars belong to another symbol")
    offset = setup.entry_ms - bars.start_ms
    if offset < 0 or offset % MINUTE_MS:
        raise ValueError("bars must start at or before the entry minute")
    e = offset // MINUTE_MS
    if e >= bars.minutes:
        return Resolution(setup.setup_id, "pending")
    window = min(setup.window_minutes, bars.minutes - e)
    model = FORWARD_PARAMS.cost_model
    label = label_trade(bars, funding or empty_funding(bars), model, tick=setup.tick, entry_index=e, side=setup.side,
                        stop_price=setup.stop, target_prices=[setup.target], window_minutes=window)
    through = bars.open_time(e + window - 1)
    if label.status != "T":
        raise ValueError(f"setup {setup.setup_id} became non-tradeable on these bars ({label.status})")
    pair = label.cells[0]
    cell = pair.pess
    if cell.outcome == "E" and window < setup.window_minutes:
        return Resolution(setup.setup_id, "open", resolved_through_ms=through, label_leverage=label.leverage,
                          wallet_ur=label.wallet_ur)
    x = e + cell.exit_offset
    if cell.outcome == "T":
        exit_ref = setup.target
    elif cell.outcome == "S":
        opened = bars.open[x]
        exit_ref = min(setup.stop, opened) if setup.side == 1 else max(setup.stop, opened)
    elif cell.outcome == "E":
        exit_ref = bars.close[x]
    elif cell.outcome == "L":
        fill_in = costs.entry_fill_price(setup.side, setup.p0, bars.high[e], bars.low[e], model)
        lp = costs.liquidation_price(setup.side, fill_in, label.leverage, model.mmr)
        exit_ref = round(lp)
    else:
        exit_ref = None  # X: no trustworthy price at the compromised minute
    optimistic = None
    if pair.opt is not None:
        optimistic = {"outcome": pair.opt.outcome, "net_ur": pair.opt.net_ur, "cost_ur": pair.opt.cost_ur,
                      "fund_ur": pair.opt.fund_ur}
    return Resolution(setup.setup_id, "ambiguous" if pair.opt is not None else cell.outcome,
                      resolved_through_ms=bars.open_time(x), exit_offset=cell.exit_offset, exit_ms=bars.open_time(x),
                      exit_ref_price=exit_ref, net_ur=cell.net_ur, cost_ur=cell.cost_ur, fund_ur=cell.fund_ur,
                      optimistic=optimistic, label_leverage=label.leverage, wallet_ur=label.wallet_ur)
