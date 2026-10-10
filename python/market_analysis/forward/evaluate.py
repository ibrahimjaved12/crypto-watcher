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
from .sigma_state import SigmaState, SigmaStateMismatch, StateSigma, empty_state, POINT_HISTORY_MS
from .wallet import WalletConfig, initial_state, adverse_path, wallet_step


def _funding(bars, events) -> FundingSeries:
    inside = sorted((int(e["calc_time_ms"]), int(e.get("interval_hours", 8)), Fraction(str(e["rate"])))
                    for e in events or () if bars.start_ms <= int(e["calc_time_ms"]) < bars.end_ms)
    return FundingSeries(tuple(t for t, _, _ in inside), tuple(h for _, h, _ in inside),
                         tuple(r for _, _, r in inside), bars.start_ms, bars.end_ms)


def market_evaluate(symbol: str, bars, funding=None, open_setups=(), *, strategy_ids,
                    from_ms: int, to_ms: int, funding_available: bool = True,
                    stage_run_id: str = "00000000-0000-0000-0000-000000000000",
                    sigma_factory=ForwardSigma, sigma_state=None, use_sigma_state=False, sigma_only=False,
                    _legacy_window=False) -> dict:
    """Evaluate one symbol from collector rows, without sizing or moving wallet balances.

    Shared records contain no wallet configuration or positions. Liquidation is derived
    from adverse mark paths by each user's wallet. Events use stage/symbol/sequence keys.
    """
    # Shared stages cannot publish paths, exits or checkpoints beyond their boundary.
    # /evaluate historically treats to_ms as the signal window only, so its private
    # composition retains the full supplied resolution history.
    if not _legacy_window:
        bars = [row for row in bars if row["open_time_ms"] <= to_ms]
    bars = bars_from_collector_rows(symbol, bars)
    apply_funding_marks(bars, funding)
    funding = _funding(bars, funding)
    funding_ok = funding_available
    incremental = None
    if use_sigma_state:
        incremental = (SigmaState.from_record(sigma_state, symbol=symbol) if sigma_state is not None
                       else empty_state(symbol, bars.start_ms, source="store_rebuild"))
        if sigma_state is not None:
            # The adapter pads back to a 4h boundary; padding is not supplied history.
            first_row_ms = bars.start_ms + next(i for i, price in enumerate(bars.open) if price > 0) * MINUTE_MS
            if bars.start_ms > incremental.as_of_ms + MINUTE_MS or first_row_ms > incremental.as_of_ms + MINUTE_MS:
                raise SigmaStateMismatch("sigma history has an omitted leading interval")
        if incremental.as_of_ms >= bars.end_ms or (not sigma_only and incremental.day is not None
                and from_ms < incremental.day * 86_400_000 - POINT_HISTORY_MS):
            raise SigmaStateMismatch("sigma state decision history mismatch")
        sigma = StateSigma(incremental, bars)
    else:
        sigma = sigma_factory(bars, FORWARD_PARAMS)
    if sigma_only:
        if incremental is None:
            raise ValueError("sigma_only requires use_sigma_state")
        return {"symbol": symbol, "sigma_state": incremental.to_record()}
    saved = [(Setup.from_dict(item["setup"]),
              Resolution(**item["resolution"]) if item.get("resolution") else None)
             for item in open_setups if item["setup"]["symbol"] == symbol]
    out_signals, out_setups, out_resolutions, wallet_events = [], [], [], []
    signals, symbol_reasons = generate_signals(symbol, bars, from_ms, to_ms, strategy_ids)
    reasons = symbol_reasons
    processed_to = min(to_ms, bars.end_ms - MINUTE_MS)
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
    paths = [adverse_path(setup.to_dict(), bars, resolutions.get(setup.setup_id))
             for setup, _ in saved + new]
    events = [{**event, "stage_run_id": stage_run_id, "symbol": symbol, "sequence": sequence}
              for sequence, event in enumerate(wallet_events)]
    events.sort(key=lambda event: (event["ms"], event["symbol"], event["sequence"]))
    return {"symbol": symbol, "signals": out_signals, "setups": out_setups,
            "resolutions": out_resolutions, "events": events, "adverse_paths": paths, "reasons": reasons,
            "processed_to_ms": processed_to, "funding_available": funding_ok,
            "versions": dict(VERSIONS), "params_hash": FORWARD_PARAMS.identity(),
            "assumptions": list(ASSUMPTIONS),
            **({"sigma_state": incremental.to_record()} if incremental is not None else {})}


def evaluate(symbols: list, *, strategy_ids, from_ms: int, to_ms: int, open_setups=(), wallet_state=None,
             wallet_config: WalletConfig = WalletConfig(), sigma_factory=ForwardSigma) -> dict:
    """Compose per-symbol market evaluation and the pure portfolio wallet step."""
    state = wallet_state or initial_state(wallet_config)
    out_signals, out_setups, out_resolutions, reasons, wallet_events = [], [], [], {}, []
    processed_to, funding_unavailable, paths = {}, [], []
    for item in symbols:
        result = market_evaluate(item["symbol"], item["rows"], item.get("funding"), open_setups,
                                 strategy_ids=strategy_ids, from_ms=from_ms, to_ms=to_ms,
                                 funding_available=item.get("funding_available", True),
                                 sigma_factory=sigma_factory, _legacy_window=True)
        paths.extend(result["adverse_paths"])
        # Legacy callers may hold positions without saved market setups. Preserve /evaluate
        # by deriving their paths locally; the shared endpoint never accepts these positions.
        known = {path["setup_id"] for path in result["adverse_paths"]}
        orphaned = [(key, value) for key, value in state["positions"].items()
                    if value["symbol"] == item["symbol"] and key not in known]
        if orphaned:
            series = bars_from_collector_rows(item["symbol"], item["rows"])
            apply_funding_marks(series, item.get("funding"))
            paths.extend(adverse_path({**value, "setup_id": key}, series, None) for key, value in orphaned)
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
    new_state, ledger = wallet_step(state, wallet_events, wallet_config, adverse_paths=paths, compatibility=True)
    return {"schema_version": 1, "versions": dict(VERSIONS), "params_hash": FORWARD_PARAMS.identity(),
            "wallet_config": wallet_config.to_record(), "assumptions": [*ASSUMPTIONS, *wallet_config.assumptions],
            "processed_to_ms": processed_to, "funding_unavailable": funding_unavailable, "signals": out_signals, "setups": out_setups,
            "resolutions": out_resolutions, "reasons": reasons, "ledger": ledger, "wallet_state": new_state}
