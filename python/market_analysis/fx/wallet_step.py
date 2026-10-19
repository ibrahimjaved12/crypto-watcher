"""One per-user forex wallet step (#445), the body of ``POST /v1/fx/forward/wallet_step``.

Pure: applies the stage's shared events (``fx.stage``: ``open``, ``close``, ``swap``) to ONE user's stored
wallet state with ``fx.wallet.step`` and adds that user's stop-outs. Liquidation depends on the user's
position size (through the stop-out price), so it is computed here and never in the shared stage.

Stop-out is found in passes, always applying the events with ``step`` from the ORIGINAL state:

1. ``step`` over the events without liquidations says which positions exist and with which leverage. A
   position that opens and closes inside the batch is no longer in the returned state, so its stop-out
   price is rebuilt from its ``open_fee`` ledger line (same ``stop_out_price`` call as ``step``).
2. ``stop_out_events`` scans the supplied bars for those positions (carried ones and the batch's), up to the
   close event's minute.
3. ``step`` over ALL events (liquidations included) is the answer. A liquidation frees margin, which can let
   a later open of the same batch succeed that pass 1 rejected, and that position needs scanning too, so the
   passes repeat until the liquidation set stops changing (a few rounds at most; no change after the second
   is the normal case). ``step`` ignores a close for a position already stopped out.

Everything is exact (strings for money and prices, ints in 10**-8); nothing here reads a clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from ..benchmark.canonical import exact_from_str, exact_to_str
from ..trading_calendar import calendar_for
from ..instruments import VenueSymbol
from .bars import fx_bars_from_rows
from .costs import FxContract, stop_out_price
from .setups import FxSetup
from .stage import FxStageInputError, _SETUP_KEYS, _cost_model, _exact, _int, _keys, _text, instrument_for
from .wallet import FxWalletConfig, WALLET_VERSION, initial_state, step, stop_out_events

MAX_ROUNDS = 8
_REQUEST_KEYS = {"state", "contracts", "events"}
_REQUEST_OPTIONAL = {"config", "symbols", "cost_model"}
_CONFIG_KEYS = {"initial_balance_e8", "risk_fraction", "max_positions", "max_exposure_multiple"}
_CONTRACT_KEYS = {"contract_size", "tick_size", "min_lot", "lot_step", "base", "quote"}
_SYMBOL_KEYS = {"symbol", "venue_symbol", "quote_to_account", "bars"}
_VENUE_KEYS = {"venue", "native_symbol", "digits", "tick_size"}
_STATE_KEYS = {"version", "balance_e8", "used_margin_e8", "seq", "positions", "done"}
_EVENT_FIELDS = {
    "open": {"setup", "quote_to_account"},
    "close": {"setup_id", "quote_to_account", "exit_fill"},
    "swap": {"symbol", "long_per_unit", "short_per_unit", "quote_to_account", "multiplier"},
}


@dataclass(frozen=True)
class _Exit:
    exit_ms: int


def _canonical(name: str, value, *, positive: bool = False) -> str:
    return exact_to_str(_exact(name, value, positive=positive))


def _config(record, contracts: dict, model) -> FxWalletConfig:
    record = _keys("config", {} if record is None else record, set(), _CONFIG_KEYS)
    defaults = FxWalletConfig()
    values = {"initial_balance_e8": defaults.initial_balance_e8, "max_positions": defaults.max_positions,
              "risk_fraction": defaults.risk_fraction, "max_exposure_multiple": defaults.max_exposure_multiple}
    for name in ("initial_balance_e8", "max_positions"):
        if name in record:
            values[name] = _int(f"config.{name}", record[name])
    for name in ("risk_fraction", "max_exposure_multiple"):
        if name in record:
            values[name] = _exact(f"config.{name}", record[name], positive=True)
    pairs = tuple((symbol, contracts[symbol]) for symbol in sorted(contracts))
    try:
        return FxWalletConfig(model=model, contracts=pairs, **values)
    except ValueError as error:
        raise FxStageInputError(f"config: {error}") from None


def _contracts(record) -> Dict[str, FxContract]:
    if not isinstance(record, dict):
        raise FxStageInputError("contracts must be an object keyed by symbol")
    out = {}
    for symbol, item in record.items():
        item = _keys(f"contracts.{symbol}", item, _CONTRACT_KEYS)
        out[_text("contracts key", symbol)] = FxContract(
            _exact(f"{symbol}.contract_size", item["contract_size"], positive=True),
            _exact(f"{symbol}.tick_size", item["tick_size"], positive=True),
            _exact(f"{symbol}.min_lot", item["min_lot"], positive=True),
            _exact(f"{symbol}.lot_step", item["lot_step"], positive=True),
            _text(f"{symbol}.base", item["base"]), _text(f"{symbol}.quote", item["quote"]))
    return out


def _state(record, config: FxWalletConfig) -> dict:
    if record is None:
        return initial_state(config)
    record = _keys("state", record, _STATE_KEYS, {"swaps"})
    if record["version"] != WALLET_VERSION:
        raise FxStageInputError(f"state.version must be {WALLET_VERSION!r}")
    for name in ("balance_e8", "used_margin_e8", "seq"):
        _int(f"state.{name}", record[name])
    if not isinstance(record["positions"], dict) or not all(isinstance(p, dict) for p in record["positions"].values()):
        raise FxStageInputError("state.positions must be an object of positions")
    for name in ("done", "swaps"):
        if name in record and not (isinstance(record[name], list) and all(type(i) is str for i in record[name])):
            raise FxStageInputError(f"state.{name} must be a list of strings")
    return record


def _events(items) -> List[dict]:
    if not isinstance(items, list):
        raise FxStageInputError("events must be a list")
    out = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise FxStageInputError(f"events[{index}] must be an object")
        kind = item.get("type")
        if kind == "liquidation":
            raise FxStageInputError("liquidation events are computed per user here and cannot be supplied")
        if kind not in _EVENT_FIELDS:
            raise FxStageInputError(f"events[{index}]: unknown event type {kind!r}")
        missing = _EVENT_FIELDS[kind] - set(item)
        if missing:
            raise FxStageInputError(f"events[{index}] ({kind}): missing {sorted(missing)}")
        _int(f"events[{index}].ms", item.get("ms"))
        # The wallet parses canonical exact text only ("-73/1000000", not "-0.000073"): normalize on a copy.
        item = {**item, "quote_to_account": _canonical(f"events[{index}].quote_to_account",
                                                       item["quote_to_account"], positive=True)}
        if kind == "open":
            setup = _keys(f"events[{index}].setup", item["setup"], _SETUP_KEYS)
            try:
                FxSetup(**setup)
            except TypeError as error:
                raise FxStageInputError(f"events[{index}].setup: {error}") from None
        elif kind == "close":
            _text(f"events[{index}].setup_id", item["setup_id"])
            item["exit_fill"] = _canonical(f"events[{index}].exit_fill", item["exit_fill"], positive=True)
        else:
            _text(f"events[{index}].symbol", item["symbol"])
            item["long_per_unit"] = _canonical(f"events[{index}].long_per_unit", item["long_per_unit"])
            item["short_per_unit"] = _canonical(f"events[{index}].short_per_unit", item["short_per_unit"])
            _int(f"events[{index}].multiplier", item["multiplier"])
        out.append(item)
    return out


def _series(items) -> dict:
    """symbol -> (FxBarSeries or None when no minute traded, quote_to_account text)."""
    if items is None:
        return {}
    if not isinstance(items, list):
        raise FxStageInputError("symbols must be a list")
    out = {}
    for item in items:
        item = _keys("symbols[]", item, _SYMBOL_KEYS)
        symbol = _text("symbols[].symbol", item["symbol"])
        if symbol in out:
            raise FxStageInputError(f"duplicate symbol {symbol}")
        quote_text = _canonical(f"{symbol}.quote_to_account", item["quote_to_account"], positive=True)
        if not isinstance(item["bars"], list):
            raise FxStageInputError(f"{symbol}.bars must be a list")
        if not item["bars"]:
            out[symbol] = (None, quote_text)
            continue
        instrument = instrument_for(symbol)
        venue = _keys(f"{symbol}.venue_symbol", item["venue_symbol"], _VENUE_KEYS)
        venue_symbol = VenueSymbol(_text(f"{symbol}.venue", venue["venue"]),
                                   _text(f"{symbol}.native_symbol", venue["native_symbol"]), instrument.instrument_id,
                                   _int(f"{symbol}.digits", venue["digits"]),
                                   _exact(f"{symbol}.venue_symbol.tick_size", venue["tick_size"], positive=True),
                                   None)
        out[symbol] = (fx_bars_from_rows(instrument, venue_symbol, calendar_for(instrument), item["bars"]), quote_text)
    return out


def _positions_during(before: dict, events: List[dict], ledger: List[dict], config: FxWalletConfig) -> dict:
    """Every position that existed at some point of the batch: the carried ones plus each open that was not
    rejected (rebuilt from its ``open_fee`` line, because a position closed inside the batch is not in the
    returned state)."""
    positions = {key: dict(value) for key, value in before["positions"].items()}
    leverage = {line["setup_id"]: line["leverage"] for line in ledger if line["type"] == "open_fee"}
    for event in events:
        if event["type"] != "open":
            continue
        setup = event["setup"]
        setup_id = setup["setup_id"]
        if setup_id in leverage and setup_id not in positions:
            fill = exact_from_str(setup["entry_fill"])
            positions[setup_id] = {
                "symbol": setup["symbol"], "side": setup["side"], "entry_ms": setup["entry_ms"],
                "stop_out_price": exact_to_str(stop_out_price(setup["side"], fill, leverage[setup_id], config.model))}
    return positions


def fx_wallet_step(request: dict) -> dict:
    request = _keys("request", request, _REQUEST_KEYS, _REQUEST_OPTIONAL)
    model = _cost_model(request.get("cost_model"))
    contracts = _contracts(request["contracts"])
    config = _config(request.get("config"), contracts, model)
    original = _state(request["state"], config)
    events = _events(request["events"])
    series = _series(request.get("symbols"))
    closes = {event["setup_id"]: _Exit(event["ms"]) for event in events if event["type"] == "close"}

    liquidations: List[dict] = []
    for _round in range(MAX_ROUNDS):
        state, ledger = step(original, events + liquidations, config)
        positions = _positions_during(original, events, ledger, config)
        found = []
        for symbol in sorted({position["symbol"] for position in positions.values()}):
            if symbol not in series:
                raise FxStageInputError(f"no bars supplied for {symbol}, which has an open position")
            bars, quote_text = series[symbol]
            if bars is not None:
                found += stop_out_events(positions, bars, closes, quote_text)
        found.sort(key=lambda event: (event["ms"], event["setup_id"]))
        if found == liquidations:
            return {"schema_version": 1, "wallet_state": state, "ledger": ledger,
                    "wallet_config": config.to_record(), "assumptions": list(config.assumptions)}
        liquidations = found
    raise ValueError("stop-out passes did not settle")


__all__ = ["MAX_ROUNDS", "fx_wallet_step"]
