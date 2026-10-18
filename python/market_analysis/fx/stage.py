"""One stateless hourly forex market stage (#397), the body of ``POST /v1/fx/forward/stage``.

The forex twin of ``forward.evaluate.market_evaluate``, built from the fx package. Pure: no I/O, no clock.
Per symbol: gateway bars (``fx_bars_from_rows``, UTC, warm-up history included) -> trading-time candles
(15/60/240) -> ``generate_fx_signals`` for closes in ``(from_ms, boundary_ms]`` -> the news blackout
(``news_blackout.blackout``; a hit goes to ``suppressed`` with reason ``news_blackout``) -> ``build_fx_setups``
per remaining signal -> ``resolve_fx_setup`` for every new tradeable setup (status ``T``) and every saved
open setup (with its previous resolution). Setups with another status are stored, never resolved. A
resolution ``X`` (a missing minute, including the entry minute) is recorded with
``payload.excluded_from_stats = true``: no fill is ever invented.

The response holds the records ``commit_fx_stage`` takes (``stage`` = ``p_stage``; ``signals``,
``suppressed``, ``setups``, ``outcomes`` and ``events`` = ``p_records``) with the column names of
``fx_market_signals`` / ``_suppressed`` / ``_setups`` / ``_outcomes``. The stage id is a UUID derived from
``(boundary_ms, params_hash, symbols)``, so a replayed request produces the same stage (and the database's
commit returns the existing one). Money stays exact: prices are scaled ints (10**8), outcomes integer
micro-R; contract, swap and quote-to-account values travel as exact decimal or fraction strings. Anything
unsupported raises ``FxStageInputError`` (a ``ValueError``); nothing is skipped silently.

Wallet events (#444) are the shared, user-independent facts the per-user wallet (``fx/wallet.py``) consumes:
``open`` for every NEW tradeable setup, ``close`` (with the resolution's ``exit_fill`` and ``outcome``) for
every resolution that became final in this stage, and ``swap`` for every rollover instant in
``(from_ms, processed_to]`` that some open position can cross (only when the symbol has ``swap_rates``). A
resolution ``X`` has no trustworthy price, so it emits no ``open`` and no ``close``: it stays excluded from
stats. Liquidation events are NOT produced here; they depend on each user's position size and are computed
per user by the wallet-step endpoint (#445). Each event is the wallet's own event dict plus ``symbol``, a
per-symbol ``sequence`` (ordered by ``(ms, swap < close < open, setup_id)``, so a replay is byte-identical) and
``stage_run_id`` (the stage id: ``commit_fx_stage`` stores the whole item and checks it).
"""
from __future__ import annotations

from dataclasses import fields
from fractions import Fraction
import hashlib
import re
import uuid
from typing import Dict, List, Optional

from .. import data_lake
from ..forward.bars_adapter import MINUTE_MS
from ..forward.outcomes import FINAL
from ..instruments import FOREX_DEFAULTS, FOREX_OPTIONAL, Instrument, VenueSymbol, forex_instrument
from ..news_blackout import DEFAULT_RULE, EventIndex, blackout
from ..trading_calendar import calendar_for
from .bars import fx_bars_from_rows
from .calendar_rows import calendar_events_from_rows
from .candles import FX_CANDLE_ANCHOR_MS, FX_TIMEFRAMES, build_fx_candles
from .costs import FX_COST_MODEL_V1, FxContract, FxCostModel, SwapRates
from .resolve import FxResolution, resolve_fx_setup
from .setups import FX_GEOMETRY_PARAMS, FX_GEOMETRY_VERSION, FxSetup, build_fx_setups, fx_params_hash
from .signals import generate_fx_signals

HOUR_MS = 3_600_000
FX_STAGE_VERSION = "fx-stage-v1"
VERSIONS = {
    "fx_stage": FX_STAGE_VERSION,
    "fx_bars": "fx-bars-v1",
    "fx_candles": f"fx-candles-anchor-{FX_CANDLE_ANCHOR_MS}",
    "signals": "signals-v1",
    "ta": "ta-v1",
    "fx_geometry": FX_GEOMETRY_VERSION,
    "fx_resolve": "fx-resolve-v1",
    "news_blackout": "news-blackout-v1",
}
# Fixed namespace for stage ids (uuid5 of "fx-stage-v1"); changing it would change every stage id.
_STAGE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "crypto-watcher:fx-stage-v1")

_REQUEST_KEYS = {"boundary_ms", "from_ms", "symbols"}
_OPTIONAL_KEYS = {"strategy_ids", "calendar_events", "cost_model"}
_SYMBOL_KEYS = {"symbol", "venue_symbol", "contract", "quote_to_account", "bars", "open_setups"}
_SYMBOL_OPTIONAL = {"swap_rates"}
_VENUE_KEYS = {"venue", "native_symbol", "digits", "tick_size"}
_CONTRACT_KEYS = {"contract_size", "tick_size", "min_lot", "lot_step", "base", "quote"}
_SWAP_KEYS = {"long_per_unit", "short_per_unit"}
_SETUP_KEYS = {field.name for field in fields(FxSetup)}
_RESOLUTION_KEYS = {field.name for field in fields(FxResolution)}
_EXACT = re.compile(r"^-?(\d+(\.\d+)?|\d+/\d+)$")


class FxStageInputError(ValueError):
    """The request does not match the stage contract."""


def _fail(message: str):
    raise FxStageInputError(message)


def _keys(name: str, value, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(value, dict):
        _fail(f"{name} must be an object")
    missing, unknown = required - set(value), set(value) - required - optional
    if missing or unknown:
        _fail(f"{name}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return value


def _int(name: str, value) -> int:
    if type(value) is not int:  # rejects bool and float
        _fail(f"{name} must be an int")
    return value


def _exact(name: str, value, *, positive: bool = False) -> Fraction:
    """An exact decimal or fraction STRING ("1.0842", "1/3"); never a float."""
    if type(value) is not str or not _EXACT.match(value):
        _fail(f"{name} must be an exact decimal or fraction string")
    result = Fraction(value)
    if positive and result <= 0:
        _fail(f"{name} must be positive")
    return result


def _text(name: str, value) -> str:
    if type(value) is not str or not value:
        _fail(f"{name} must be a non-empty string")
    return value


def instrument_for(symbol: str) -> Instrument:
    """The known forex instrument (metals, energy, indices carry their own base/quote), else a six-letter pair."""
    for instrument in (*FOREX_DEFAULTS, *FOREX_OPTIONAL):
        if instrument.instrument_id.split(":", 1)[1] == symbol:
            return instrument
    try:
        return forex_instrument(symbol)
    except ValueError as error:
        raise FxStageInputError(f"unsupported forex symbol {symbol!r}: {error}") from None


def _cost_model(record) -> FxCostModel:
    if record is None:
        return FX_COST_MODEL_V1
    allowed = {field.name for field in fields(FxCostModel)}
    record = _keys("cost_model", record, {"version"}, allowed - {"version"})
    values = {}
    for name, value in record.items():
        default = getattr(FX_COST_MODEL_V1, name)
        if type(default) is Fraction:
            values[name] = _exact(f"cost_model.{name}", value)
        elif type(default) is int:
            values[name] = _int(f"cost_model.{name}", value)
        else:
            values[name] = _text(f"cost_model.{name}", value)
    try:
        return FxCostModel(**values)
    except ValueError as error:
        raise FxStageInputError(f"cost_model: {error}") from None


def stage_params_hash(geometry_hash: str, strategy_ids: Optional[List[str]]) -> str:
    """Everything that changes a stage's decisions: geometry and cost model (inside ``geometry_hash``), the
    blackout rule and the strategy selection."""
    rule = DEFAULT_RULE
    record = {"fx_stage": FX_STAGE_VERSION, "geometry": geometry_hash,
              "blackout": {"min_impact": rule.min_impact.value,
                           "before_ms": {impact.value: ms for impact, ms in rule.before_ms.items()},
                           "after_ms": {impact.value: ms for impact, ms in rule.after_ms.items()}},
              "strategy_ids": None if strategy_ids is None else sorted(strategy_ids)}
    return hashlib.sha256(data_lake.canonical_json(record).encode("ascii")).hexdigest()


def stage_id(boundary_ms: int, params_hash: str, symbols: List[str]) -> str:
    return str(uuid.uuid5(_STAGE_NAMESPACE, f"{boundary_ms}|{params_hash}|{','.join(sorted(symbols))}"))


def _signal_record(signal, instrument_id: str, sessions) -> dict:
    return {**signal.to_dict(), "instrument_id": instrument_id, "sessions": list(sessions), "candle_version": None}


def _setup_record(setup: FxSetup) -> dict:
    record = setup.to_dict()
    return {key: record[key] for key in ("setup_id", "signal_id", "strategy_id", "version", "symbol", "side",
                                         "horizon_min", "signal_ms", "entry_ms", "k", "rr", "status",
                                         "geometry_version", "params_hash")} | {"payload": record}


def _outcome_record(resolution: FxResolution) -> dict:
    record = resolution.to_dict()
    final = resolution.status in FINAL
    return {"setup_id": resolution.setup_id, "status": resolution.status, "final": final,
            "resolved_through_ms": resolution.resolved_through_ms, "exit_ms": resolution.exit_ms,
            "net_ur": resolution.net_ur, "cost_ur": resolution.cost_ur, "fund_ur": resolution.fund_ur,
            "payload": {**record, "excluded_from_stats": resolution.status == "X"}}


_EVENT_ORDER = {"swap": 0, "close": 1, "open": 2}


def _wallet_events(symbol: str, calendar, resolved: list, new_ids: set, quote_text: str, swap_text,
                   from_ms: int, processed_to_ms: int) -> List[dict]:
    """The wallet events of one symbol (see the module docstring), numbered ``sequence`` 0.. in a fixed order.

    ``quote_text`` and ``swap_text`` are the request's own exact strings (what the wallet parses). A swap is
    emitted for a rollover instant only if some non-X setup entered before it and is not yet finally exited
    before it (an exit exactly at the instant is charged, like the resolver). The window ends at the last
    processed minute, so a rollover is never announced before the bars that decide who held through it."""
    events = []
    for setup, previous, resolution in resolved:
        if resolution.status == "X":
            continue  # no trustworthy price: no wallet trade exists
        if setup.setup_id in new_ids:
            events.append({"type": "open", "ms": setup.entry_ms, "setup": setup.to_dict(),
                           "quote_to_account": quote_text})
        if resolution.final and not (previous is not None and previous.final):
            events.append({"type": "close", "ms": resolution.exit_ms, "setup_id": setup.setup_id,
                           "quote_to_account": quote_text, "exit_fill": resolution.exit_fill,
                           "outcome": resolution.status})
    if swap_text is not None and processed_to_ms > from_ms:
        for instant, multiplier in calendar.rollover_events(from_ms, processed_to_ms):
            if any(r.status != "X" and setup.entry_ms < instant and (not r.final or r.exit_ms >= instant)
                   for setup, _previous, r in resolved):
                events.append({"type": "swap", "ms": instant, "long_per_unit": swap_text["long_per_unit"],
                               "short_per_unit": swap_text["short_per_unit"], "quote_to_account": quote_text,
                               "multiplier": multiplier})
    events.sort(key=lambda ev: (ev["ms"], _EVENT_ORDER[ev["type"]],
                                ev.get("setup_id") or ev.get("setup", {}).get("setup_id") or ""))
    return [{**event, "symbol": symbol, "sequence": index} for index, event in enumerate(events)]


def _open_setups(name: str, items, symbol: str) -> list:
    if not isinstance(items, list):
        _fail(f"{name} must be a list")
    out = []
    for index, item in enumerate(items):
        item = _keys(f"{name}[{index}]", item, {"setup", "previous"})
        setup_dict = _keys(f"{name}[{index}].setup", item["setup"], _SETUP_KEYS)
        if setup_dict["symbol"] != symbol:
            _fail(f"{name}[{index}] belongs to {setup_dict['symbol']!r}, not {symbol!r}")
        if setup_dict["status"] != "T":
            _fail(f"{name}[{index}] is not a tradeable setup (status {setup_dict['status']!r})")
        try:
            setup = FxSetup(**setup_dict)
        except TypeError as error:
            raise FxStageInputError(f"{name}[{index}].setup: {error}") from None
        previous = item["previous"]
        if previous is not None:
            previous = _keys(f"{name}[{index}].previous", previous, {"setup_id", "status"},
                             _RESOLUTION_KEYS - {"setup_id", "status"})
            if previous["setup_id"] != setup.setup_id:
                _fail(f"{name}[{index}].previous belongs to another setup")
            previous = FxResolution(**previous)
        out.append((setup, previous))
    return out


def _symbol_stage(item: dict, *, boundary_ms: int, from_ms: int, strategy_ids, model: FxCostModel,
                  events: EventIndex) -> dict:
    item = _keys("symbol", item, _SYMBOL_KEYS, _SYMBOL_OPTIONAL)
    symbol = _text("symbol.symbol", item["symbol"])
    instrument = instrument_for(symbol)
    calendar = calendar_for(instrument)
    venue = _keys(f"{symbol}.venue_symbol", item["venue_symbol"], _VENUE_KEYS)
    contract_rec = _keys(f"{symbol}.contract", item["contract"], _CONTRACT_KEYS)
    contract = FxContract(_exact(f"{symbol}.contract.contract_size", contract_rec["contract_size"], positive=True),
                          _exact(f"{symbol}.contract.tick_size", contract_rec["tick_size"], positive=True),
                          _exact(f"{symbol}.contract.min_lot", contract_rec["min_lot"], positive=True),
                          _exact(f"{symbol}.contract.lot_step", contract_rec["lot_step"], positive=True),
                          _text(f"{symbol}.contract.base", contract_rec["base"]),
                          _text(f"{symbol}.contract.quote", contract_rec["quote"]))
    if (contract.base, contract.quote) != (instrument.base, instrument.quote):
        _fail(f"{symbol}: contract {contract.base}/{contract.quote} does not match {instrument.base}/{instrument.quote}")
    venue_symbol = VenueSymbol(_text(f"{symbol}.venue", venue["venue"]), _text(f"{symbol}.native_symbol",
                               venue["native_symbol"]), instrument.instrument_id,
                               _int(f"{symbol}.digits", venue["digits"]),
                               _exact(f"{symbol}.venue_symbol.tick_size", venue["tick_size"], positive=True),
                               contract.contract_size)
    if venue_symbol.tick_size != contract.tick_size:
        _fail(f"{symbol}: venue tick_size differs from the contract tick_size")
    rates = None
    if item.get("swap_rates") is not None:
        swap = _keys(f"{symbol}.swap_rates", item["swap_rates"], _SWAP_KEYS)
        rates = SwapRates(_exact(f"{symbol}.swap_rates.long_per_unit", swap["long_per_unit"]),
                          _exact(f"{symbol}.swap_rates.short_per_unit", swap["short_per_unit"]))
    quote_to_account = _exact(f"{symbol}.quote_to_account", item["quote_to_account"], positive=True)
    rows = item["bars"]
    if not isinstance(rows, list):
        _fail(f"{symbol}.bars must be a list")
    # Bars up to and including the boundary minute (its entry row), like market_evaluate's shared stage.
    rows = [row for row in rows if not (isinstance(row, dict) and type(row.get("open_time_ms")) is int
                                        and row["open_time_ms"] > boundary_ms)]
    saved = _open_setups(f"{symbol}.open_setups", item["open_setups"], symbol)

    bars = fx_bars_from_rows(instrument, venue_symbol, calendar, rows)
    candles = {minutes: build_fx_candles(bars, minutes, calendar) for minutes in FX_TIMEFRAMES}
    batch = generate_fx_signals(instrument, bars, candles, calendar, from_ms, boundary_ms, strategy_ids)

    signals, suppressed, setups, new = [], [], [], []
    for entry in batch.suppressed:
        suppressed.append({**_signal_record(entry.signal, instrument.instrument_id, entry.sessions),
                           "reason": entry.reason, "detail": {}})
    for signal in batch.signals:
        tags = batch.sessions.get(signal.signal_id, ())
        decision = blackout(instrument, signal.signal_ms, events)
        if decision.active:
            suppressed.append({**_signal_record(signal, instrument.instrument_id, tags), "reason": "news_blackout",
                               "detail": {"event_ids": [event.event_id for event in decision.events],
                                          "window_start_ms": decision.window_start_ms,
                                          "window_end_ms": decision.window_end_ms}})
            continue
        signals.append(_signal_record(signal, instrument.instrument_id, tags))
        for setup in build_fx_setups(signal, candles, bars, calendar, model):
            setups.append(_setup_record(setup))
            if setup.status == "T":
                new.append((setup, None))
    suppressed.sort(key=lambda record: (record["signal_ms"], record["strategy_id"], record["side"]))

    outcomes, resolved = [], []
    for setup, previous in saved + new:
        if setup.entry_ms < bars.start_ms:
            _fail(f"{symbol}: bars start after the entry of open setup {setup.setup_id}")
        resolution = resolve_fx_setup(setup, bars, calendar, model, contract, rates=rates,
                                      quote_to_account=quote_to_account, previous=previous)
        outcomes.append(_outcome_record(resolution))
        resolved.append((setup, previous, resolution))
    processed_to_ms = min(boundary_ms, bars.end_ms - MINUTE_MS)
    swap_text = item.get("swap_rates")
    events = _wallet_events(symbol, calendar, resolved, {setup.setup_id for setup, _ in new},
                            item["quote_to_account"], swap_text if rates is not None else None,
                            from_ms, processed_to_ms)
    return {"symbol": symbol, "signals": signals, "suppressed": suppressed, "setups": setups, "outcomes": outcomes,
            "events": events, "reasons": dict(sorted(batch.reasons.items())), "processed_to_ms": processed_to_ms}


def fx_market_stage(request: dict) -> dict:
    request = _keys("request", request, _REQUEST_KEYS, _OPTIONAL_KEYS)
    boundary_ms = _int("boundary_ms", request["boundary_ms"])
    from_ms = _int("from_ms", request["from_ms"])
    if boundary_ms < 0 or boundary_ms % HOUR_MS:
        _fail("boundary_ms must be a non-negative UTC hour boundary")
    if not 0 <= from_ms <= boundary_ms:
        _fail("from_ms must be between 0 and boundary_ms")
    strategy_ids = request.get("strategy_ids")
    if strategy_ids is not None:
        if not isinstance(strategy_ids, list) or not strategy_ids:
            _fail("strategy_ids must be a non-empty list when given")
        strategy_ids = [_text("strategy_ids[]", item) for item in strategy_ids]
    model = _cost_model(request.get("cost_model"))
    calendar_rows = request.get("calendar_events", [])
    try:
        events = EventIndex(calendar_events_from_rows(calendar_rows))
    except ValueError as error:
        raise FxStageInputError(f"calendar_events: {error}") from None
    items = request["symbols"]
    if not isinstance(items, list) or not items:
        _fail("symbols must be a non-empty list")
    names = [item.get("symbol") if isinstance(item, dict) else None for item in items]
    if len(set(names)) != len(names):
        _fail("duplicate symbol")

    geometry_hash = fx_params_hash(FX_GEOMETRY_PARAMS, model)
    params_hash = stage_params_hash(geometry_hash, strategy_ids)
    per_symbol = [_symbol_stage(item, boundary_ms=boundary_ms, from_ms=from_ms, strategy_ids=strategy_ids,
                                model=model, events=events) for item in items]
    symbols = [result["symbol"] for result in per_symbol]
    processed = {result["symbol"]: result["processed_to_ms"] for result in per_symbol}
    versions = {**VERSIONS, "cost_model": model.version, "cost_model_identity": model.identity()}
    stage = {"id": stage_id(boundary_ms, params_hash, symbols), "boundary_ms": boundary_ms, "versions": versions,
             "params_hash": params_hash,
             "payload": {"from_ms": from_ms, "symbols": sorted(symbols), "geometry_params_hash": geometry_hash,
                         "processed_to_ms": processed,
                         "reasons": {result["symbol"]: result["reasons"] for result in per_symbol},
                         "counts": {result["symbol"]: {name: len(result[name]) for name in
                                                       ("signals", "suppressed", "setups", "outcomes", "events")}
                                    for result in per_symbol}},
             "freshness": {}}
    return {
        "schema_version": 1,
        "versions": versions,
        "params_hash": params_hash,
        "processed_to_ms": min(processed.values()),
        "stage": stage,
        "signals": [record for result in per_symbol for record in result["signals"]],
        "suppressed": [record for result in per_symbol for record in result["suppressed"]],
        "setups": [record for result in per_symbol for record in result["setups"]],
        "outcomes": [record for result in per_symbol for record in result["outcomes"]],
        "events": [{**event, "stage_run_id": stage["id"]} for result in per_symbol for event in result["events"]],
    }


__all__ = ["FX_STAGE_VERSION", "FxStageInputError", "VERSIONS", "fx_market_stage", "instrument_for", "stage_id",
           "stage_params_hash"]
