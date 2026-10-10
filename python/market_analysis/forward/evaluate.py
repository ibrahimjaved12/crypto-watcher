"""One stateless forward evaluation (the body of ``POST /v1/forward/evaluate``).

Input: per symbol the completed 1-minute collector rows (and optional funding events), the
strategy ids, the decision window ``(from_ms, to_ms]``, the still-open setups with all their saved
fields, and the wallet state. Output: new signals and setups, resolutions of every open or new
tradeable setup, wallet ledger entries and the new wallet state, plus versions, assumptions and
``processed_to_ms`` (the last decision time whose entry minute existed; pass it as the next
``from_ms``). Nothing is read from or written to any store.

Sigma: ``ewma-robust-hcal`` (P16); a signal without that sigma yet emits no setup and the symbol's
``reasons`` gets ``sigma: no_sigma: ...``.

Funding: with ``funding_available: false`` for a symbol (the caller's funding fetch failed), a
resolution whose window ``(entry, exit]`` contains a possible funding time (every UTC hour
boundary: Binance settles on 1, 2, 4 or 8-hour boundaries) is reported as ``open`` and not
finalised; funding is never filled with zeros. Such symbols are listed in ``funding_unavailable``.
"""
from __future__ import annotations

from fractions import Fraction

from ..benchmark.bars import infer_tick
from ..benchmark.funding import FundingSeries
from ..benchmark.scan import next_compromised
from . import VERSIONS
from .bars_adapter import ASSUMPTIONS, MINUTE_MS, apply_funding_marks, bars_from_collector_rows
from .outcomes import Resolution, resolve_setup

HOUR_MS = 3_600_000


def crosses_funding_time(entry_ms: int, exit_ms: int) -> bool:
    """True when (entry_ms, exit_ms] contains a UTC hour boundary (a possible funding settlement)."""
    return exit_ms // HOUR_MS * HOUR_MS > entry_ms
from .setups import FORWARD_PARAMS, NO_SIGMA, ForwardSigma, Setup, build_setup
from .signals import generate_signals
from .wallet import WalletConfig, initial_state, liquidation_events, step


def _funding(bars, events) -> FundingSeries:
    inside = sorted((int(e["calc_time_ms"]), int(e.get("interval_hours", 8)), Fraction(str(e["rate"])))
                    for e in events or () if bars.start_ms <= int(e["calc_time_ms"]) < bars.end_ms)
    return FundingSeries(tuple(t for t, _, _ in inside), tuple(h for _, h, _ in inside),
                         tuple(r for _, _, r in inside), bars.start_ms, bars.end_ms)


def evaluate(symbols: list, *, strategy_ids, from_ms: int, to_ms: int, open_setups=(), wallet_state=None,
             wallet_config: WalletConfig = WalletConfig()) -> dict:
    """``symbols``: [{"symbol", "rows", "funding"?}]; ``open_setups``: [{"setup": dict, "resolution": dict|None}]."""
    saved = [(Setup.from_dict(item["setup"]),
              Resolution(**item["resolution"]) if item.get("resolution") else None) for item in open_setups]
    state = wallet_state or initial_state(wallet_config)
    out_signals, out_setups, out_resolutions, reasons, wallet_events = [], [], [], {}, []
    processed_to = {}
    funding_unavailable = []
    liquidation_inputs = []
    for item in symbols:
        symbol = item["symbol"]
        bars = bars_from_collector_rows(symbol, item["rows"])
        apply_funding_marks(bars, item.get("funding"))  # settlement marks from the funding history, when given
        funding = _funding(bars, item.get("funding"))
        funding_ok = item.get("funding_available", True)
        if not funding_ok:
            funding_unavailable.append(symbol)
        signals, symbol_reasons = generate_signals(symbol, bars, from_ms, to_ms, strategy_ids)
        reasons[symbol] = symbol_reasons
        processed_to[symbol] = min(to_ms, bars.end_ms - MINUTE_MS)
        sigma = ForwardSigma(bars, FORWARD_PARAMS)
        tick = infer_tick(bars)
        nc = next_compromised(bars)
        new = []
        no_sigma = 0
        for signal in signals:
            setups = build_setup(signal, bars, sigma, tick=tick, next_comp=nc)
            if setups == NO_SIGMA:
                no_sigma += 1
            out_signals.append(signal.to_dict())
            for setup in setups or ():
                out_setups.append(setup.to_dict())
                if setup.status == "T":
                    new.append((setup, None))
                    wallet_events.append({"type": "open", "ms": setup.entry_ms, "setup": setup.to_dict()})
        if no_sigma:  # never another model: the signal stands, no setup is emitted (P16)
            symbol_reasons["sigma"] = (f"no_sigma: {no_sigma} signal(s) before {FORWARD_PARAMS.sigma_model} "
                                       "history exists (60 days of completed windows)")
        resolutions = {}
        for setup, previous in [pair for pair in saved if pair[0].symbol == symbol] + new:
            if setup.entry_ms < bars.start_ms:
                continue  # its bars are not in this request; keep the previous resolution
            resolution = resolve_setup(setup, bars, funding, previous)
            if (not funding_ok and resolution.final and not (previous is not None and previous.final)
                    and crosses_funding_time(setup.entry_ms, resolution.exit_ms)):
                resolution = Resolution(setup.setup_id, "open", resolved_through_ms=resolution.resolved_through_ms,
                                        label_leverage=resolution.label_leverage, wallet_ur=resolution.wallet_ur)
            out_resolutions.append(resolution.to_dict())
            resolutions[setup.setup_id] = resolution.to_dict()
            if resolution.final and not (previous is not None and previous.final):
                wallet_events.append({"type": "close", "ms": resolution.exit_ms, "setup_id": setup.setup_id,
                                      "outcome": resolution.status, "exit_ref_price": resolution.exit_ref_price})
        for calc_time, rate, _ in zip(funding.calc_time_ms, funding.rate, funding.interval_hours):
            mark = bars.mark_open[(calc_time - bars.start_ms) // MINUTE_MS]
            if mark > 0:
                wallet_events.append({"type": "funding", "ms": calc_time, "symbol": symbol, "rate": str(rate),
                                      "mark": mark})
        liquidation_inputs.append((bars, resolutions))
    # Liquidation needs the positions as they will be after this step's opens: price them from the
    # setups (the wallet's leverage and liquidation price do not depend on the size).
    opened, _ = step(state, [ev for ev in wallet_events if ev["type"] == "open"], wallet_config)
    positions = {**state["positions"], **opened["positions"]}
    for bars, resolutions in liquidation_inputs:
        wallet_events += liquidation_events(positions, bars, resolutions, wallet_config)
    new_state, ledger = step(state, wallet_events, wallet_config)
    return {"schema_version": 1, "versions": dict(VERSIONS), "params_hash": FORWARD_PARAMS.identity(),
            "wallet_config": wallet_config.to_record(), "assumptions": [*ASSUMPTIONS, *wallet_config.assumptions],
            "processed_to_ms": processed_to, "funding_unavailable": funding_unavailable, "signals": out_signals, "setups": out_setups,
            "resolutions": out_resolutions, "reasons": reasons, "ledger": ledger, "wallet_state": new_state}
