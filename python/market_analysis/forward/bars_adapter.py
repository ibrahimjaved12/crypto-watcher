"""Collector 1-minute rows (``collector_recent_candles``, doubles) -> benchmark ``BarSeries`` (integers).

Scaling rule (explicit, tested): a price or volume double ``x`` becomes
``Decimal(repr(x)) * 10**8`` rounded half-even to an integer. ``repr`` is the shortest decimal
that round-trips the double, so a value the exchange published with at most 8 decimals is
recovered exactly (0.1 -> 10_000_000, never 9_999_999).

Series layout: the first minute is the 240-minute UTC epoch boundary at or before the first row
(``candles.build_candles`` needs aligned starts) and the last minute is the last row. A minute
without a row is MISSING in every price column with the flags the data lake gives a minute with
neither trades nor a kline row (NO_AGGTRADES, KLINE_ROW_MISSING, MARK_MISSING, INDEX_MISSING,
PREMIUM_MISSING), so it is compromised exactly as in the benchmark.

Assumptions (recorded in every forward response):
- ``mark-proxy``: the collector stores no mark price; mark OHLC = trade OHLC. Liquidation checks and
  funding settlement marks therefore use trade prices.
- No index/premium (INDEX_MISSING | PREMIUM_MISSING on every minute; not compromised flags).
- No taker-buy volume or trade count (MISSING): order-flow strategies cannot run live.

Rows are validated strictly; any violation raises ``CollectorRowError`` with a code.
"""
from __future__ import annotations

from array import array
from decimal import ROUND_HALF_EVEN, Decimal
from math import isfinite

from .. import data_lake
from ..benchmark.bars import MISSING, BarSeries
from ..benchmark.candles import TIMEFRAMES, build_candles

SCALE_EXPONENT = 8
MINUTE_MS = data_lake.MINUTE_MS
ALIGN_MS = 240 * MINUTE_MS
PROVIDER = "binance-usdm"
REST_ENDPOINT = "/fapi/v1/klines"
WS_ENDPOINT = "wss://fstream.binance.com/market/stream"
MISSING_MINUTE_FLAGS = (data_lake.FLAG_NO_AGGTRADES | data_lake.FLAG_KLINE_ROW_MISSING | data_lake.FLAG_MARK_MISSING
                        | data_lake.FLAG_INDEX_MISSING | data_lake.FLAG_PREMIUM_MISSING)
PRESENT_MINUTE_FLAGS = data_lake.FLAG_INDEX_MISSING | data_lake.FLAG_PREMIUM_MISSING
ASSUMPTIONS = ("mark-proxy: mark OHLC = trade OHLC (no mark stream collected)",
               "no index/premium, taker-buy volume or trade count in collector rows")
_MAX_SCALED = 2 ** 63 - 1


class CollectorRowError(ValueError):
    """A collector row is malformed, mis-aligned, out of order or has inconsistent provenance."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def to_scaled(value) -> int:
    """Double -> integer at 10**8: shortest round-trip decimal, then round half to even."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise CollectorRowError("non_finite", f"not a finite number: {value!r}")
    scaled = (Decimal(repr(value)) if isinstance(value, float) else Decimal(value)).scaleb(SCALE_EXPONENT)
    result = int(scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if not 0 <= result <= _MAX_SCALED:
        raise CollectorRowError("out_of_range", f"{value!r} does not fit a non-negative int64 at 10**8")
    return result


def _check_row(symbol: str, row: dict, index: int) -> tuple:
    where = f"row {index}"
    if not isinstance(row, dict):
        raise CollectorRowError("malformed", f"{where} is not an object")
    if row.get("provider", PROVIDER) != PROVIDER or row.get("symbol", symbol) != symbol \
            or row.get("price_type", "trade") != "trade" or row.get("timeframe_minutes", 1) != 1:
        raise CollectorRowError("identity", f"{where} is not a {PROVIDER} {symbol} 1-minute trade candle")
    open_ms = row.get("open_time_ms")
    if type(open_ms) is not int or open_ms < 0 or open_ms % MINUTE_MS:
        raise CollectorRowError("alignment", f"{where} open_time_ms is not a UTC minute boundary")
    close_ms = row.get("close_time_ms")
    if close_ms is not None and close_ms != open_ms + MINUTE_MS - 1:
        raise CollectorRowError("alignment", f"{where} close_time_ms is not open + 59.999 s")
    transport, endpoint, event = row.get("transport"), row.get("endpoint"), row.get("source_event_at_ms")
    if not ((transport == "rest" and endpoint in (None, REST_ENDPOINT) and event is None)
            or (transport == "websocket" and endpoint in (None, WS_ENDPOINT) and type(event) is int)):
        raise CollectorRowError("provenance", f"{where} transport/endpoint/source event are inconsistent")
    o, h, l, c = (to_scaled(row.get(name)) for name in ("open", "high", "low", "close"))
    if min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c):
        raise CollectorRowError("ohlc", f"{where} OHLC is not a valid candle")
    volume = to_scaled(row.get("volume", 0))
    return open_ms, o, h, l, c, volume


def bars_from_collector_rows(symbol: str, rows) -> BarSeries:
    """Strictly increasing completed 1-minute rows of one symbol -> a contiguous BarSeries."""
    data_lake.validate_symbol(symbol)
    parsed = [_check_row(symbol, row, index) for index, row in enumerate(rows)]
    if not parsed:
        raise CollectorRowError("empty", "no rows")
    for previous, current in zip(parsed, parsed[1:]):
        if current[0] <= previous[0]:
            raise CollectorRowError("order", f"open time {current[0]} is not after {previous[0]}")
    start = parsed[0][0] - parsed[0][0] % ALIGN_MS
    minutes = (parsed[-1][0] - start) // MINUTE_MS + 1
    prices = {name: array("q", [MISSING]) * minutes for name in ("open", "high", "low", "close")}
    volume = array("q", [MISSING]) * minutes
    flags = array("H", [MISSING_MINUTE_FLAGS]) * minutes
    for open_ms, o, h, l, c, v in parsed:
        i = (open_ms - start) // MINUTE_MS
        for name, value in zip(("open", "high", "low", "close"), (o, h, l, c)):
            prices[name][i] = value
        volume[i] = v
        flags[i] = PRESENT_MINUTE_FLAGS
    unknown = array("q", [MISSING]) * minutes
    return BarSeries(symbol, start, minutes, prices["open"], prices["high"], prices["low"], prices["close"],
                     volume, unknown, array("q", unknown), array("q", prices["open"]), array("q", prices["high"]),
                     array("q", prices["low"]), array("q", prices["close"]), flags)


def candles_from_bars(bars: BarSeries) -> dict:
    """{15, 60, 240: CandleSeries} via ``candles.build_candles`` (complete candles only)."""
    return {minutes: build_candles(bars, minutes) for minutes in TIMEFRAMES}


def missing_minutes(bars: BarSeries, first_ms: int | None = None) -> int:
    """Minutes without a row from ``first_ms`` (default: the first row) to the end."""
    first = 0 if first_ms is None else max(0, (first_ms - bars.start_ms) // MINUTE_MS)
    if first_ms is None:
        while first < bars.minutes and bars.open[first] == MISSING:
            first += 1
    return sum(1 for i in range(first, bars.minutes) if bars.open[i] == MISSING)
