#!/usr/bin/env python3
"""Probe of the Dukascopy hourly tick files (#402 part 1, docs/forex.md "Historical data").

Checks the format assumptions of python/market_analysis/fx/dukascopy.py against the real files before any
importer is built. It publishes and stores nothing (no release, no artifact, no file outside the job
summary) and prints only names, counts and aggregate numbers, never response bytes.

Politeness: at most 40 requests, one at a time, 1.5 s apart, a fixed User-Agent, a 20 s timeout and no
retries. A 403, 429 or 503 stops the probe at once with a summary line, so the host is never hammered.
All dates are 2024 (the development stretch), so no hidden-test month is read.

Evidence:
1. Month base: EURUSD 2024-01-16 10h UTC on the zero-based path (/2024/00/16/) and the one-based path
   (/2024/01/16/): HTTP status and byte size of each.
2. EURUSD, USDJPY, XAUUSD 2024-01-16 10h on the base that answered: tick count, first/last tick time,
   the first 3 raw integer ask/bid, the decoded bid with the table's divisor, min/max decoded bid and the
   median decoded spread in points.
3. Time base: EURUSD Sundays 2024-01-07 and 2024-07-07, hours 20..23 UTC: ticks per hour and the first
   tick of the first non-empty hour (the FX week opens 22:00 UTC in winter, 21:00 UTC in summer).
4. Minute bars: bars_from_ticks for EURUSD 2024-01-16 10h: bar count and the first and last bar.
A failed check prints ``CHECK FAILED: <name>`` and the probe continues with the next item.

Standard library only (urllib.request, time, lzma) plus the repo's pure decoder.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import lzma
import os
from pathlib import Path
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

from market_analysis.fx.dukascopy import POINT_DIVISORS, RECORD, bars_from_ticks, decode_bi5, hour_url  # noqa: E402

USER_AGENT = "crypto-watcher-research/1"
TIMEOUT_S = 20
GAP_S = 1.5
MAX_REQUESTS = 40
STOP_STATUSES = (403, 429, 503)
DIGITS = {"EURUSD": 5, "GBPUSD": 5, "USDJPY": 3, "XAUUSD": 3}  # spread points in the divisor's units


class StopProbe(Exception):
    pass


class Fetcher:
    def __init__(self, lines):
        self.lines = lines
        self.count = 0
        self.last_at = None

    def get(self, url: str):
        """(status, body). status is an int, or 'network' / 'timeout'. Never retried."""
        if self.count >= MAX_REQUESTS:
            raise StopProbe(f"request budget of {MAX_REQUESTS} reached")
        if self.last_at is not None:
            time.sleep(max(0.0, GAP_S - (time.monotonic() - self.last_at)))
        self.count += 1
        self.last_at = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            if error.code in STOP_STATUSES:
                raise StopProbe(f"HTTP {error.code} from the host; probe stopped to avoid hammering it") from None
            return error.code, b""
        except TimeoutError:
            return "timeout", b""
        except (urllib.error.URLError, OSError):
            return "network", b""


def hour_ms(year, month, day, hour):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp()) * 1000


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


def raw_records(body: bytes, limit: int):
    """The first `limit` raw (ask, bid) integers, read directly from the payload."""
    payload = lzma.decompress(body) if body else b""
    usable = payload[: len(payload) - len(payload) % RECORD.size]
    return [(ask, bid) for _, ask, bid, _, _ in list(RECORD.iter_unpack(usable))[:limit]]


def decode(lines, name, body, start_ms, divisor):
    try:
        return decode_bi5(body, start_ms, divisor)
    except (ValueError, lzma.LZMAError) as error:
        reason = str(error).split(":")[-1].strip() or type(error).__name__
        lines.append(f"CHECK FAILED: {name} ({reason})")
        return None


def month_base(fetch, lines):
    lines += ["", "## 1. Month base (EURUSD 2024-01-16 10h UTC)", ""]
    answered = {}
    for zero_based in (True, False):
        label = "zero-based /2024/00/16/" if zero_based else "one-based /2024/01/16/"
        status, body = fetch.get(hour_url("EURUSD", 2024, 1, 16, 10, zero_based_month=zero_based))
        lines.append(f"- {label}: HTTP {status}, {len(body)} bytes")
        if status == 200 and body:
            answered[zero_based] = body
    if True in answered and False in answered:
        lines.append("- both paths answered; using the zero-based one")
    if not answered:
        lines.append("CHECK FAILED: month base (neither path returned data)")
        return None, None
    base = True if True in answered else False
    lines.append(f"- month base: {'zero-based' if base else 'one-based'}")
    return base, answered[base]


def symbols_item(fetch, lines, base, eurusd_body):
    lines += ["", "## 2. Symbols (2024-01-16 10h UTC)", ""]
    start = hour_ms(2024, 1, 16, 10)
    for symbol in ("EURUSD", "USDJPY", "XAUUSD"):
        if symbol == "EURUSD" and eurusd_body is not None:
            status, body = 200, eurusd_body  # reused from item 1, no second request
        else:
            status, body = fetch.get(hour_url(symbol, 2024, 1, 16, 10, zero_based_month=base))
        lines.append(f"### {symbol}")
        lines.append(f"- HTTP {status}, {len(body)} bytes, divisor {POINT_DIVISORS[symbol]}")
        if status != 200 or not body:
            lines.append(f"CHECK FAILED: {symbol} data")
            continue
        try:
            raw = raw_records(body, 3)
        except lzma.LZMAError:
            lines.append(f"CHECK FAILED: {symbol} decompress")
            continue
        ticks = decode(lines, f"{symbol} records", body, start, POINT_DIVISORS[symbol])
        lines.append(f"- first 3 raw ask/bid integers: {', '.join(f'{a}/{b}' for a, b in raw) or 'none'}")
        if not ticks:
            lines.append(f"- ticks: {0 if ticks == [] else 'not decoded'}")
            continue
        bids = [tick.bid for tick in ticks]
        spreads = [float((tick.ask - tick.bid) * 10 ** DIGITS[symbol]) for tick in ticks]
        lines += [
            f"- ticks: {len(ticks)}; first {iso(ticks[0].time_ms)}, last {iso(ticks[-1].time_ms)}",
            f"- first decoded bid: {float(ticks[0].bid)}",
            f"- decoded bid min/max: {float(min(bids))} / {float(max(bids))}",
            f"- median decoded spread: {statistics.median(spreads):g} points ({DIGITS[symbol]} digits)",
        ]
        if any(spread < 0 for spread in spreads):
            lines.append(f"CHECK FAILED: {symbol} ask below bid")


def time_base(fetch, lines, base):
    lines += ["", "## 3. Time base (EURUSD Sundays, UTC hours 20-23)", ""]
    for (year, month, day), season in (((2024, 1, 7), "winter, expected open 22:00 UTC"),
                                        ((2024, 7, 7), "summer, expected open 21:00 UTC")):
        counts, first = [], None
        for hour in (20, 21, 22, 23):
            status, body = fetch.get(hour_url("EURUSD", year, month, day, hour, zero_based_month=base))
            ticks = decode(lines, f"{year}-{month:02d}-{day:02d} {hour}h records", body, hour_ms(year, month, day, hour),
                           POINT_DIVISORS["EURUSD"]) if status == 200 else []
            counts.append(f"{hour}h: {'HTTP ' + str(status) if status != 200 else len(ticks or [])}")
            if ticks and first is None:
                first = ticks[0].time_ms
        lines.append(f"- {year}-{month:02d}-{day:02d} ({season}): {'; '.join(counts)}")
        lines.append(f"  first tick: {iso(first) if first is not None else 'none in 20-23h'}")


def minute_bars(lines, body):
    lines += ["", "## 4. Minute bars (EURUSD 2024-01-16 10h UTC)", ""]
    ticks = decode(lines, "bars records", body, hour_ms(2024, 1, 16, 10), POINT_DIVISORS["EURUSD"])
    if not ticks:
        lines.append("CHECK FAILED: minute bars (no ticks)")
        return
    try:
        bars = bars_from_ticks(ticks, DIGITS["EURUSD"])
    except ValueError as error:
        lines.append(f"CHECK FAILED: minute bars ({str(error).split(':')[-1].strip()})")
        return
    describe = lambda bar: (f"{iso(bar['open_time_ms'])} O {bar['open']} H {bar['high']} L {bar['low']} "
                            f"C {bar['close']} spread {bar['spread_points']} pts, {bar['tick_volume']} ticks")
    lines += [f"- bars: {len(bars)}", f"- first: {describe(bars[0])}", f"- last: {describe(bars[-1])}"]


def run(lines):
    fetch = Fetcher(lines)
    lines += ["# Dukascopy tick probe", "",
              f"At most {MAX_REQUESTS} requests, {GAP_S} s apart, no retries; 403/429/503 stops the probe."]
    try:
        base, eurusd = month_base(fetch, lines)
        if base is None:
            lines.append("Items 2-4 skipped: no month base answered.")
            return
        symbols_item(fetch, lines, base, eurusd)
        time_base(fetch, lines, base)
        minute_bars(lines, eurusd)
    except StopProbe as stop:
        lines += ["", f"**Probe stopped:** {stop}."]
    finally:
        lines += ["", f"Requests made: {fetch.count}."]


def write_summary(path, lines):
    text = "\n".join(lines) + "\n"
    print(text)
    if path:
        Path(path).write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe the Dukascopy hourly tick files (#402).")
    parser.add_argument("--summary", help="Markdown summary path (default: $DUKASCOPY_PROBE_SUMMARY)")
    args = parser.parse_args(argv)
    lines: list = []
    run(lines)
    write_summary(args.summary or os.environ.get("DUKASCOPY_PROBE_SUMMARY"), lines)
    return 0


if __name__ == "__main__":
    sys.exit(main())
