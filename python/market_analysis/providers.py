"""Public GET requests only. Preserve app exchange order and USDT symbols."""
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import Candle, MINUTE, positive

PROVIDERS = ("Binance", "OKX", "Kraken")
SUPPORTED_SYMBOLS = frozenset(
    "BTCUSDT ETHUSDT DOGEUSDT SOLUSDT XRPUSDT ADAUSDT BNBUSDT AVAXUSDT "
    "LINKUSDT MATICUSDT DOTUSDT LTCUSDT TRXUSDT ATOMUSDT NEARUSDT APTUSDT "
    "ARBUSDT OPUSDT SUIUSDT TONUSDT".split()
)


def request_url(provider, symbol, interval):
    # One extra row versus TypeScript because the current candle is discarded.
    limit = 62 if interval == 1 else 98
    if provider == "Binance":
        base, params = "https://api.binance.com/api/v3/klines", {
            "symbol": symbol, "interval": f"{interval}m", "limit": limit}
    elif provider == "OKX":
        base, params = "https://www.okx.com/api/v5/market/candles", {
            "instId": symbol[:-4] + "-USDT", "bar": f"{interval}m", "limit": limit}
    elif provider == "Kraken":
        base, params = "https://api.kraken.com/0/public/OHLC", {
            "pair": symbol, "interval": interval}
    else:
        raise ValueError("unknown provider")
    return base + "?" + urlencode(params)


def fetch_json(url):
    request = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "CryptoWatcher-Analysis/1.0"})
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def parse(provider, body, interval):
    if provider == "Binance":
        rows = body
    elif provider == "OKX":
        if body.get("code") != "0":
            raise ValueError(f"OKX: {body.get('msg', 'API error')}")
        rows = body.get("data")
    elif provider == "Kraken":
        if body.get("error"):
            raise ValueError("Kraken: " + ", ".join(body["error"]))
        result = body.get("result", {})
        keys = [key for key in result if key != "last"]
        if len(keys) != 1:
            raise ValueError("expected exactly one Kraken pair")
        rows = result[keys[0]]
    else:
        raise ValueError("unknown provider")
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty or invalid candle response")
    candles = []
    for index, row in enumerate(rows):
        # int() must not silently truncate a fractional timestamp.
        timestamp = str(row[0])
        if not timestamp.isdigit():
            raise ValueError("invalid exchange timestamp")
        opened = int(timestamp) * (1000 if provider == "Kraken" else 1)
        complete = True
        if provider == "Binance":
            if int(row[6]) != opened + interval * MINUTE - 1:
                raise ValueError("unexpected Binance close timestamp")
        elif provider == "OKX":
            if row[8] not in ("0", "1"):
                raise ValueError("invalid OKX completion flag")
            complete = row[8] == "1"
        else:
            # Kraken documents its final row as always uncommitted.
            complete = index != len(rows) - 1
        candles.append(Candle(opened, positive(row[4]), complete))
    return sorted(candles, key=lambda candle: candle.open_ms)


def load(provider, symbol, fetch=fetch_json):
    return {interval: parse(provider, fetch(request_url(provider, symbol, interval)), interval)
            for interval in (1, 15)}
