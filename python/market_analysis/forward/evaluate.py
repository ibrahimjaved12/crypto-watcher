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
from .wallet import WalletConfig, initial_state, liquidation_events, position_terms, wallet_step


def _funding(bars, events) -> FundingSeries:
    inside = sorted((int(e["calc_time_ms"]), int(e.get("interval_hours", 8)), Fraction(str(e["rate"])))
                    for e in events or () if bars.start_ms <= int(e["calc_time_ms"]) < bars.end_ms)
    return FundingSeries(tuple(t for t, _, _ in inside), tuple(h for _, h, _ in inside),
                         tuple(r for _, _, r in inside), bars.start_ms, bars.end_ms)


def market_evaluate(symbol: str, bars, funding=None, open_setups=(), *, strategy_ids,
                    from_ms: int, to_ms: int, funding_available: bool = True,
                    positions=None, wallet_config: WalletConfig = WalletConfig(),
                    sigma_factory=ForwardSigma) -> dict:
    """Evaluate one symbol from collector rows, without sizing or moving wallet balances.

    Positions supply previously persisted liquidation terms. New setups emit liquidation
    candidates independent of sizing; wallet_step applies the legacy portfolio preflight.
    Events are JSON records ordered by (ms, symbol, sequence).
    """
    bars = bars_from_collector_rows(symbol, bars)
    apply_funding_marks(bars, funding)
    funding = _funding(bars, funding)
    funding_ok = funding_available
    saved = [(Setup.from_dict(item["setup"]),
              Resolution(**item["resolution"]) if item.get("resolution") else None)
             for item in open_setups if item["setup"]["symbol"] == symbol]
    out_signals, out_setups, out_resolutions, wallet_events = [], [], [], []
    signals, symbol_reasons = generate_signals(symbol, bars, from_ms, to_ms, strategy_ids)
    reasons = symbol_reasons
    processed_to = min(to_ms, bars.end_ms - MINUTE_MS)
    sigma = sigma_factory(bars, FORWARD_PARAMS)
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
    for setup, previous in saved + new:
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
    # Liquidation prices depend on geometry/config, never on quantity or available balance.
    candidates = {key: dict(value) for key, value in (positions or {}).items()
                  if value["symbol"] == symbol}
    for setup, _ in new:
        record = setup.to_dict()
        terms = position_terms(record, wallet_config)
        if terms is not None and setup.setup_id not in candidates:
            candidates[setup.setup_id] = {"symbol": symbol, "side": setup.side,
                                         "entry_ms": setup.entry_ms,
                                         "liquidation_price": str(terms[2])}
    wallet_events += liquidation_events(candidates, bars, resolutions, wallet_config)
    events = [{**event, "symbol": symbol, "sequence": sequence}
              for sequence, event in enumerate(wallet_events)]
    events.sort(key=lambda event: (event["ms"], event["symbol"], event["sequence"]))
    return {"symbol": symbol, "signals": out_signals, "setups": out_setups,
            "resolutions": out_resolutions, "events": events, "reasons": reasons,
            "processed_to_ms": processed_to, "funding_available": funding_ok}


def evaluate(symbols: list, *, strategy_ids, from_ms: int, to_ms: int, open_setups=(), wallet_state=None,
             wallet_config: WalletConfig = WalletConfig(), sigma_factory=ForwardSigma) -> dict:
    """Compose per-symbol market evaluation and the pure portfolio wallet step."""
    state = wallet_state or initial_state(wallet_config)
    out_signals, out_setups, out_resolutions, reasons, wallet_events = [], [], [], {}, []
    processed_to, funding_unavailable = {}, []
    for item in symbols:
        result = market_evaluate(item["symbol"], item["rows"], item.get("funding"), open_setups,
                                 strategy_ids=strategy_ids, from_ms=from_ms, to_ms=to_ms,
                                 funding_available=item.get("funding_available", True),
                                 positions=state["positions"], wallet_config=wallet_config, sigma_factory=sigma_factory)
        symbol = item["symbol"]
        out_signals.extend(result["signals"])
        out_setups.extend(result["setups"])
        out_resolutions.extend(result["resolutions"])
        reasons[symbol] = result["reasons"]
        processed_to[symbol] = result["processed_to_ms"]
        if not result["funding_available"]:
            funding_unavailable.append(symbol)
        # Preserve the old stable tie order, including caller symbol order, for /evaluate.
        for event in sorted(result["events"], key=lambda event: event["sequence"]):
            wallet_events.append({**event, "compatibility_order": len(wallet_events)})
    new_state, ledger = wallet_step(state, wallet_events, wallet_config)
    return {"schema_version": 1, "versions": dict(VERSIONS), "params_hash": FORWARD_PARAMS.identity(),
            "wallet_config": wallet_config.to_record(), "assumptions": [*ASSUMPTIONS, *wallet_config.assumptions],
            "processed_to_ms": processed_to, "funding_unavailable": funding_unavailable, "signals": out_signals, "setups": out_setups,
            "resolutions": out_resolutions, "reasons": reasons, "ledger": ledger, "wallet_state": new_state}
