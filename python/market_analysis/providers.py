"""Public perpetual-futures providers with ordered exchange fallback."""
import json
from math import isfinite
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .core import Candle, MINUTE, positive

SOURCE = "binance-usdm"
OKX_SOURCE = "okx-usdt-swap"
KRAKEN_SOURCE = "kraken-futures"
PROVIDERS = (SOURCE, OKX_SOURCE, KRAKEN_SOURCE)
ANALYSIS_PROVIDERS = PROVIDERS
SUPPORTED_SYMBOLS = frozenset(
    "BTCUSDT ETHUSDT DOGEUSDT SOLUSDT XRPUSDT ADAUSDT BNBUSDT AVAXUSDT "
    "LINKUSDT POLUSDT DOTUSDT LTCUSDT TRXUSDT ATOMUSDT NEARUSDT APTUSDT "
    "ARBUSDT OPUSDT SUIUSDT TONUSDT".split()
)
BASE_URLS = {
    SOURCE: "https://fapi.binance.com",
    OKX_SOURCE: "https://www.okx.com",
    KRAKEN_SOURCE: "https://futures.kraken.com",
}
ENDPOINTS = {
    SOURCE: "/fapi/v1/klines",
    OKX_SOURCE: "/api/v5/market/candles",
    KRAKEN_SOURCE: "/api/charts/v1/trade/:symbol/:resolution",
}


def instrument_id(symbol):
    if symbol not in SUPPORTED_SYMBOLS:
        raise ValueError("unsupported futures contract")
    return f"{SOURCE}:{symbol}"


def instrument(symbol):
    return {"id": instrument_id(symbol), "exchange": "binance", "native_symbol": symbol,
            "market_type": "futures", "contract_type": "perpetual",
            "base_asset": symbol.removesuffix("USDT"), "quote_asset": "USDT",
            "margin_asset": "USDT", "settlement_asset": "USDT", "linear": True,
            "contract_multiplier": 1}


def native_symbol(provider, symbol):
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    base = symbol.removesuffix("USDT")
    if provider == OKX_SOURCE:
        return f"{base}-USDT-SWAP"
    if provider == KRAKEN_SOURCE:
        return f"PF_{'XBT' if base == 'BTC' else base}USD"
    return symbol


def provider_endpoint(provider):
    try:
        return ENDPOINTS[provider]
    except KeyError:
        raise ValueError("unknown provider") from None


def exchange_info_url(provider=SOURCE, symbol=None):
    if provider == SOURCE:
        return BASE_URLS[provider] + "/fapi/v1/exchangeInfo"
    if provider == OKX_SOURCE:
        native = native_symbol(provider, symbol)
        return BASE_URLS[provider] + "/api/v5/public/instruments?" + urlencode(
            {"instType": "SWAP", "instId": native})
    if provider == KRAKEN_SOURCE:
        return None
    raise ValueError("unknown provider")


def request_url(provider, symbol, interval, limit=None):
    native = native_symbol(provider, symbol)
    limit = limit or (62 if interval == 1 else 98)
    if provider == OKX_SOURCE:
        resolution = f"{interval // 60}H" if interval >= 60 else f"{interval}m"
        return BASE_URLS[provider] + ENDPOINTS[provider] + "?" + urlencode(
            {"instId": native, "bar": resolution, "limit": limit})
    if provider == KRAKEN_SOURCE:
        resolution = f"{interval // 60}h" if interval >= 60 else f"{interval}m"
        return (BASE_URLS[provider] + f"/api/charts/v1/trade/{quote(native)}/{resolution}?"
                + urlencode({"count": limit}))
    resolution = f"{interval // 60}h" if interval >= 60 else f"{interval}m"
    return BASE_URLS[provider] + ENDPOINTS[provider] + "?" + urlencode(
        {"symbol": native, "interval": resolution, "limit": limit})


def fetch_json(url):
    request = Request(url, headers={"Accept": "application/json",
                                    "User-Agent": "CryptoWatcher-Analysis/1.0"})
    with urlopen(request, timeout=8) as response:
        return json.load(response)


def validate_exchange_info(body, symbol, provider=SOURCE):
    expected = instrument(symbol)
    if provider == KRAKEN_SOURCE:
        return expected
    if provider == OKX_SOURCE:
        if (not isinstance(body, dict) or body.get("code") != "0"
                or not isinstance(body.get("data"), list) or not body["data"]):
            raise ValueError("invalid exchange information")
        item = body["data"][0]
        if (item.get("instId") != native_symbol(provider, symbol)
                or item.get("instType") != "SWAP" or item.get("ctType") != "linear"
                or item.get("settleCcy") != "USDT" or item.get("state") != "live"):
            raise ValueError("inactive or incompatible futures contract")
        return expected
    if provider != SOURCE:
        raise ValueError("unknown provider")
    if not isinstance(body, dict) or not isinstance(body.get("symbols"), list):
        raise ValueError("invalid exchange information")
    item = next((value for value in body["symbols"] if value.get("symbol") == symbol), None)
    if (not item or item.get("status") != "TRADING"
            or item.get("contractType") != "PERPETUAL" or item.get("pair") != symbol
            or item.get("baseAsset") != expected["base_asset"]
            or item.get("quoteAsset") != "USDT" or item.get("marginAsset") != "USDT"):
        raise ValueError("inactive or incompatible futures contract")
    filters = {value.get("filterType"): value for value in item.get("filters", [])
               if isinstance(value, dict)}
    try:
        rules = {"price_tick": str(positive(filters["PRICE_FILTER"]["tickSize"])),
                 "quantity_step": str(positive(filters["LOT_SIZE"]["stepSize"])),
                 "min_quantity": str(positive(filters["LOT_SIZE"]["minQty"])),
                 "min_notional": str(positive(filters["MIN_NOTIONAL"]["notional"]))}
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid futures contract filters") from None
    if type(item.get("onboardDate")) is not int or type(item.get("deliveryDate")) is not int:
        raise ValueError("invalid futures contract dates")
    return dict(expected, status="TRADING", listed_at=item["onboardDate"],
                expires_at=None if item["deliveryDate"] >= 4102444800000 else item["deliveryDate"],
                **rules)


def parse(provider, body, interval):
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    if provider == OKX_SOURCE:
        rows = body.get("data") if isinstance(body, dict) and body.get("code") == "0" else None
    elif provider == KRAKEN_SOURCE:
        rows = body.get("candles") if isinstance(body, dict) else None
    else:
        rows = body
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty or invalid candle response")
    candles = []
    for row in rows:
        if provider == KRAKEN_SOURCE:
            if not isinstance(row, dict):
                raise ValueError("invalid candle row")
            opened, close = row.get("time"), row.get("close")
        else:
            if not isinstance(row, list) or len(row) < (9 if provider == OKX_SOURCE else 7):
                raise ValueError("invalid candle row")
            opened, close = row[0], row[4]
            if provider == SOURCE and int(row[6]) != int(opened) + interval * MINUTE - 1:
                raise ValueError("unexpected Binance futures close timestamp")
            if provider == OKX_SOURCE and row[8] not in ("0", "1"):
                raise ValueError("invalid OKX candle state")
        timestamp = str(opened)
        if not timestamp.isdigit() or int(timestamp) % (interval * MINUTE):
            raise ValueError("invalid exchange timestamp")
        candles.append(Candle(int(timestamp), positive(close), True))
    return sorted(candles, key=lambda candle: candle.open_ms)


def source_instrument(provider, symbol):
    native = native_symbol(provider, symbol)
    return {"instrument_id": f"{provider}:{native}", "exchange": provider,
            "native_symbol": native, "market_type": "futures",
            "contract_type": "perpetual"}


def parse_technical(provider, body, interval):
    """Parse provider OHLCV without converting one provider into another."""
    if provider not in PROVIDERS:
        raise ValueError("unknown provider")
    if provider == OKX_SOURCE:
        rows = body.get("data") if isinstance(body, dict) and body.get("code") == "0" else None
    elif provider == KRAKEN_SOURCE:
        rows = body.get("candles") if isinstance(body, dict) else None
    else:
        rows = body
    if not isinstance(rows, list) or not rows:
        raise ValueError("empty or invalid candle response")
    duration = interval * MINUTE
    candles = []
    for row in rows:
        if provider == KRAKEN_SOURCE:
            if not isinstance(row, dict):
                raise ValueError("invalid candle row")
            opened = row.get("time")
            values = (row.get("open"), row.get("high"), row.get("low"),
                      row.get("close"), row.get("volume"))
            complete = True
        else:
            if not isinstance(row, list) or len(row) < (9 if provider == OKX_SOURCE else 7):
                raise ValueError("invalid candle row")
            opened = row[0]
            values = (row[1], row[2], row[3], row[4], row[6] if provider == OKX_SOURCE else row[5])
            complete = row[8] == "1" if provider == OKX_SOURCE else True
            if provider == SOURCE and int(row[6]) != int(opened) + duration - 1:
                raise ValueError("unexpected Binance futures close timestamp")
            if provider == OKX_SOURCE and row[8] not in ("0", "1"):
                raise ValueError("invalid OKX candle state")
        timestamp = str(opened)
        if not timestamp.isdigit() or int(timestamp) % duration:
            raise ValueError("invalid exchange timestamp")
        try:
            open_price, high, low, close = (float(positive(value)) for value in values[:4])
            volume = float(values[4])
        except (TypeError, ValueError, ArithmeticError):
            raise ValueError("invalid OHLCV") from None
        if not isfinite(volume) or volume < 0:
            raise ValueError("invalid OHLCV")
        from .technical import TechnicalCandle
        candles.append(TechnicalCandle(int(timestamp), open_price, high, low, close,
                                       volume, complete))
    return tuple(sorted(candles, key=lambda candle: candle.open_ms))


def load(provider, symbol, fetch=fetch_json):
    metadata_url = exchange_info_url(provider, symbol)
    if metadata_url:
        validate_exchange_info(fetch(metadata_url), symbol, provider)
    return {interval: parse(provider, fetch(request_url(provider, symbol, interval)), interval)
            for interval in (1, 15)}
