"""MT5 spike check (#393): do broker demo candles reach Python on Linux through the Wine container?

Run from the repo root after ``pip install mt5linux`` with the container up (see mt5/README.md):

    python mt5/spike_check.py [--host localhost] [--port 8001] [--symbols EURUSD,GBPUSD,USDJPY,XAUUSD]

It reads only prices and symbol specifications and prints one JSON document to paste back. It never
calls ``account_info``, never prints a login, balance, password or server name, and never trades.

UNVERIFIED against the image: every section runs in its own try/except and records
``{"error": "<type>: <message>"}`` instead of aborting.

Clock: ``delta_hours`` is now (UTC) minus the newest M1 bar's time read as UTC epoch seconds. While
the market is closed this only measures how old the last bar is. The broker-clock offset can be read
only while the market is open: it then shows as a delta close to a whole number of hours that does
not grow between runs.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from mt5linux import MetaTrader5

SYMBOL_FIELDS = ("digits", "point", "trade_tick_size", "trade_tick_value", "trade_contract_size",
                 "volume_min", "volume_step", "spread", "trade_mode")
RATE_FIELDS = ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
MAX_NAMES_PER_BASE = 5


def error(exc: Exception) -> dict:
    return {"error": f"{type(exc).__name__}: {exc}"}


def plain(value):
    """RPyC/numpy values -> JSON-safe Python numbers or strings."""
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    for cast in (int, float):
        try:
            converted = cast(value)
            if converted == value:
                return converted
        except (TypeError, ValueError):
            pass
    return str(value)


def iso(seconds) -> str:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc).isoformat()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--symbols", default="EURUSD,GBPUSD,USDJPY,XAUUSD")
    args = parser.parse_args()
    bases = [base.strip() for base in args.symbols.split(",") if base.strip()]

    report: dict = {}
    mt5 = None
    try:
        try:
            mt5 = MetaTrader5(host=args.host, port=args.port)
        except Exception as exc:
            report["connect"] = error(exc)
            return

        try:
            ok = bool(mt5.initialize())
            info = mt5.terminal_info()
            report["initialize"] = {"ok": ok, "terminal": None if info is None else {
                name: plain(getattr(info, name, None)) for name in ("connected", "trade_allowed", "build", "company")
            }}
            if not ok:
                report["initialize"]["last_error"] = plain(mt5.last_error())
        except Exception as exc:
            report["initialize"] = error(exc)

        matches: dict = {}
        try:
            names = [str(symbol.name) for symbol in (mt5.symbols_get() or ())]
            for base in bases:
                matches[base] = [name for name in names if name.startswith(base)][:MAX_NAMES_PER_BASE]
            report["symbols"] = matches
        except Exception as exc:
            report["symbols"] = error(exc)

        newest_bar = None
        details: dict = {}
        for base in bases:
            if not matches.get(base):
                details[base] = {"error": "no matching symbol"}
                continue
            name = matches[base][0]
            entry: dict = {"name": name}
            try:
                info = mt5.symbol_info(name)
                entry["info"] = None if info is None else {field: plain(getattr(info, field, None))
                                                           for field in SYMBOL_FIELDS}
            except Exception as exc:
                entry["info"] = error(exc)
            try:
                rates = mt5.copy_rates_from_pos(name, mt5.TIMEFRAME_M1, 0, 5)
                bars = []
                for rate in (rates if rates is not None else ()):
                    bar = {field: plain(rate[field]) for field in RATE_FIELDS}
                    bar["time_iso"] = iso(bar["time"])
                    bars.append(bar)
                    newest_bar = bar["time"] if newest_bar is None else max(newest_bar, bar["time"])
                entry["rates_m1"] = bars
                if rates is None:
                    entry["rates_last_error"] = plain(mt5.last_error())
            except Exception as exc:
                entry["rates_m1"] = error(exc)
            details[base] = entry
        report["details"] = details

        try:
            now = datetime.now(timezone.utc)
            report["clock"] = {
                "now_utc": now.isoformat(),
                "newest_bar_time_iso": None if newest_bar is None else iso(newest_bar),
                "delta_hours": None if newest_bar is None else round((now.timestamp() - int(newest_bar)) / 3600, 1),
            }
        except Exception as exc:
            report["clock"] = error(exc)
    finally:
        if mt5 is not None:
            try:
                mt5.shutdown()
            except Exception as exc:
                report["shutdown"] = error(exc)
        print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
