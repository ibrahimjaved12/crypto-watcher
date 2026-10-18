"""Dukascopy hourly tick files -> ticks -> 1-minute BID bars (#402 part 1, research only).

Pure: stdlib only, no network, no clock. ``scripts/research/dukascopy_probe.py`` fetches real files and
prints the evidence; nothing here has been checked against them yet.

Assumed format of one ``<HH>h_ticks.bi5`` file (UNVERIFIED until the probe has run):
- LZMA-compressed; the payload is a sequence of 20-byte big-endian records ``>IIIff``: millisecond offset
  from the start of the UTC hour (u32), ask and bid as scaled integers (u32), ask and bid volume (f32).
- The URL month is zero-based (January = ``00``); ``hour_url`` can build both forms for the probe.
- The hour in the path is UTC.
- Prices are the integer divided by a per-symbol point divisor (``POINT_DIVISORS``).

What this module guarantees regardless: exact prices (``Fraction``, never float division), offsets inside
the hour, no zero prices, records of exactly 20 bytes, ticks in time order for bars, and bar rows in the
shape ``fx/bars.py``'s ``fx_bars_from_rows`` checks (``venue`` and ``native_symbol`` are identity fields
the caller adds; they are not invented here). No importer exists yet.
"""
from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from fractions import Fraction
from typing import Dict, List, Sequence

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
HOUR_MS = 3_600_000
MINUTE_MS = 60_000
RECORD = struct.Struct(">IIIff")

# UNVERIFIED: the probe prints the raw integers next to the decoded prices so the owner can confirm.
POINT_DIVISORS: Dict[str, int] = {"EURUSD": 100000, "GBPUSD": 100000, "USDJPY": 1000, "XAUUSD": 1000}


@dataclass(frozen=True)
class Tick:
    time_ms: int
    bid: Fraction
    ask: Fraction
    bid_volume: float
    ask_volume: float


def hour_url(symbol: str, year: int, month: int, day: int, hour: int, *, zero_based_month: bool = True) -> str:
    """URL of one UTC hour of ticks. ``month`` is the calendar month (1..12); the path month is
    ``month - 1`` when ``zero_based_month`` (UNVERIFIED; the probe tries both)."""
    if not 1 <= month <= 12 or not 1 <= day <= 31 or not 0 <= hour <= 23 or year < 1970:
        raise ValueError("year, month (1..12), day (1..31) or hour (0..23) out of range")
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must be alphanumeric")
    path_month = month - 1 if zero_based_month else month
    return f"{BASE_URL}/{symbol.upper()}/{year:04d}/{path_month:02d}/{day:02d}/{hour:02d}h_ticks.bi5"


def decode_bi5(raw: bytes, hour_start_ms: int, point_divisor: int) -> List[Tick]:
    """Decode one hour file. Empty input or an empty payload is an hour without ticks."""
    if type(point_divisor) is not int or point_divisor <= 0:
        raise ValueError("point_divisor must be a positive int")
    if not raw:
        return []
    payload = lzma.decompress(raw)
    if not payload:
        return []
    if len(payload) % RECORD.size:
        raise ValueError(f"payload length {len(payload)} is not a multiple of {RECORD.size}")
    ticks = []
    for index, (offset, ask_raw, bid_raw, ask_volume, bid_volume) in enumerate(RECORD.iter_unpack(payload)):
        if offset >= HOUR_MS:
            raise ValueError(f"record {index}: offset {offset} ms is past the hour")
        if ask_raw == 0 or bid_raw == 0:
            raise ValueError(f"record {index}: zero price")
        ticks.append(Tick(hour_start_ms + offset, Fraction(bid_raw, point_divisor), Fraction(ask_raw, point_divisor),
                          bid_volume, ask_volume))
    return ticks


def bars_from_ticks(ticks: Sequence[Tick], digits: int) -> List[dict]:
    """1-minute BID bars (UTC minutes that have ticks): first/max/min/last bid, the spread of the minute's
    last tick in points (``round((ask - bid) * 10**digits)``) and the tick count as ``tick_volume``."""
    if type(digits) is not int or digits < 0:
        raise ValueError("digits must be an int >= 0")
    bars: List[dict] = []
    current = None
    previous_ms = None
    for index, tick in enumerate(ticks):
        if previous_ms is not None and tick.time_ms < previous_ms:
            raise ValueError(f"tick {index} is earlier than the tick before it")
        previous_ms = tick.time_ms
        minute = tick.time_ms - tick.time_ms % MINUTE_MS
        if current is None or current["minute"] != minute:
            current = {"minute": minute, "open": tick.bid, "high": tick.bid, "low": tick.bid, "close": tick.bid,
                       "last": tick, "count": 0}
            bars.append(current)
        current["high"] = max(current["high"], tick.bid)
        current["low"] = min(current["low"], tick.bid)
        current["close"] = tick.bid
        current["last"] = tick
        current["count"] += 1
    rows = []
    for bar in bars:
        last = bar["last"]
        spread_points = round((last.ask - last.bid) * 10 ** digits)
        if spread_points < 0:
            raise ValueError(f"minute {bar['minute']}: ask below bid")
        rows.append({
            "time_basis": "utc",
            "transport": "history",
            "open_time_ms": bar["minute"],
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "spread_points": int(spread_points),
            "tick_volume": bar["count"],
        })
    return rows


__all__ = ["BASE_URL", "POINT_DIVISORS", "RECORD", "Tick", "bars_from_ticks", "decode_bi5", "hour_url"]
