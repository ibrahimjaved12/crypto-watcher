"""MT5 gateway (#452): completed 1-minute BID bars, symbol specs and the clock over HTTP.

Runs NEXT TO the MetaTrader 5 terminal (a Windows VM with the official ``MetaTrader5`` package, or Linux with
``mt5linux``); the forex collector (#453) on the host is its only client. It is read-only market data: it
initializes the terminal without credentials (the owner signs the terminal in by hand), never trades and never
reads account data. One file, standard library plus the MT5 module, compatible with Python 3.9.

Routes (GET, JSON; errors are ``{"error": "<code>"}`` with the HTTP status). Everything except ``/health``
needs ``Authorization: Bearer <token>``:

- ``/health``: ``{ok, terminal_connected, offset_known, now_utc_ms}``.
- ``/v1/clock``: ``{now_utc_ms, server_offset_seconds, method, observed_at_ms, market_open_hint}``.
- ``/v1/symbols``: the names the terminal knows.
- ``/v1/specs?symbols=A,B``: the symbol fields the spec store needs; ``null`` for an unknown name.
- ``/v1/bars?symbol=NAME&from_ms=..&to_ms=..``: completed one-minute bars with open time in ``[from_ms, to_ms)``.

Broker time. MT5 returns "server time" as plain numbers. Converting it to UTC needs the broker server's offset,
which is never guessed: it is either set by the owner (``MT5_SERVER_UTC_OFFSET_SECONDS``) or detected from a
FRESH tick: ``tick.time - now_utc`` rounded to a multiple of 900 seconds must leave a residual of at most 60
seconds, and the tick time must have CHANGED since the gateway's previous reading of that symbol (a stale tick
can pass the residual rule by chance, a live feed keeps producing new tick times; "changed", not "greater", so a
broker clock moved back by daylight saving time is still detected). Each detection is stored as
``{observed_at_ms, offset_seconds}`` in a small JSON file (written atomically). A bars request is served only
when the offset is known for its range: all stored observations inside the range must agree (otherwise
``409 offset_changed``), and with no observation or override it is ``503 offset_unknown``. The offset can
change twice a year when the broker server moves its clock for daylight saving time; with an override set,
that change is NOT noticed, so the owner must update the override.

Threading. Requests are served one at a time on the main thread, because the MT5 bridge must be called from the
thread that initialized it (a threaded server freezes the process on the Windows package). A client that
connects and sends nothing is dropped after 10 seconds so it cannot block the others.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

MINUTE_MS = 60_000
MAX_BARS_PER_REQUEST = 5000
MAX_RANGE_MS = MAX_BARS_PER_REQUEST * MINUTE_MS
MAX_TARGET_LENGTH = 2048
MAX_QUERY_LENGTH = 1024
MAX_SPEC_SYMBOLS = 50
ROUND_SECONDS = 900  # real broker offsets are whole quarter hours
MAX_RESIDUAL_SECONDS = 60
MAX_OFFSET_SECONDS = 14 * 3600  # UTC-12 .. UTC+14 covers every civil time zone
DETECT_INTERVAL_MS = MINUTE_MS
OFFSET_FRESH_MS = 15 * MINUTE_MS  # how old a detection may be and still count as "known" in /health and /v1/clock
MAX_OBSERVATIONS = 2000  # about a day and a half at one detection a minute
REINIT_INTERVAL_MS = 30_000
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9._#&-]{1,32}$")
INT_PATTERN = re.compile(r"^[0-9]{1,16}$")

INT_SPEC_FIELDS = ("digits", "trade_mode", "swap_mode", "swap_rollover3days")
FLOAT_SPEC_FIELDS = ("point", "trade_tick_size", "trade_tick_value", "trade_contract_size", "volume_min",
                     "volume_max", "volume_step", "swap_long", "swap_short")
TEXT_SPEC_FIELDS = ("currency_base", "currency_profit", "currency_margin")
SPEC_FIELDS = ("digits", "point", "trade_tick_size", "trade_tick_value", "trade_contract_size", "volume_min",
               "volume_max", "volume_step", "currency_base", "currency_profit", "currency_margin", "trade_mode",
               "swap_mode", "swap_long", "swap_short", "swap_rollover3days")


class HttpError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def rounded_offset(difference_seconds: float) -> Optional[int]:
    """The broker offset implied by ``tick.time - now_utc`` (seconds), or ``None``.

    The difference is rounded to the nearest multiple of 900 s; it counts only when the residual is at most
    60 s (a fresh tick: the tick is at most about a minute away from "now") and the offset is a real civil
    offset (within UTC-14h .. UTC+14h)."""
    rounded = int(round(difference_seconds / ROUND_SECONDS)) * ROUND_SECONDS
    if abs(difference_seconds - rounded) > MAX_RESIDUAL_SECONDS or abs(rounded) > MAX_OFFSET_SECONDS:
        return None
    return rounded


def _get(source: Any, name: str) -> Any:
    """A field of an MT5 namedtuple/record or a mapping; ``None`` when absent."""
    if isinstance(source, dict):
        return source.get(name)
    try:
        return source[name]  # numpy structured rows
    except (TypeError, IndexError, KeyError, ValueError):
        return getattr(source, name, None)


def _int(value: Any) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _float(value: Any) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class OffsetStore:
    """Offset observations in a JSON file. Writes go to a temporary file in the same directory, are flushed
    and fsynced, then replace the file, so a crash leaves either the old or the new content."""

    def __init__(self, path: Optional[str]) -> None:
        self.path = path
        self.observations = []  # type: List[Dict[str, int]]
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                items = data.get("observations", []) if isinstance(data, dict) else []
                self.observations = [{"observed_at_ms": int(item["observed_at_ms"]),
                                      "offset_seconds": int(item["offset_seconds"])}
                                     for item in items if isinstance(item, dict)]
            except (OSError, ValueError, KeyError, TypeError):
                self.observations = []  # an unreadable file is "nothing observed", never a guess

    def add(self, observed_at_ms: int, offset_seconds: int) -> None:
        self.observations.append({"observed_at_ms": observed_at_ms, "offset_seconds": offset_seconds})
        del self.observations[:-MAX_OBSERVATIONS]
        self._write()

    def _write(self) -> None:
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        descriptor, temporary = tempfile.mkstemp(prefix=".mt5-offsets-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "observations": self.observations}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def latest(self) -> Optional[Dict[str, int]]:
        return self.observations[-1] if self.observations else None

    def within(self, from_ms: int, to_ms: int) -> List[Dict[str, int]]:
        return [item for item in self.observations if from_ms <= item["observed_at_ms"] <= to_ms]


class Gateway:
    """The gateway logic with the MT5 module injected (tests pass a fake). ``handle`` is socket-free."""

    def __init__(self, mt5: Any, clock=time.time, state_path: Optional[str] = None, token: Optional[str] = None,
                 offset_override_seconds: Optional[int] = None) -> None:
        if offset_override_seconds is not None and (
                offset_override_seconds % ROUND_SECONDS or abs(offset_override_seconds) > MAX_OFFSET_SECONDS):
            raise ValueError("the offset override must be a multiple of 900 seconds within +-14 hours")
        if token is not None and not TOKEN_PATTERN.match(token):
            raise ValueError("the token must match ^[A-Za-z0-9_-]{32,256}$")
        self.mt5 = mt5
        self.clock = clock
        self.token = token
        self.override = offset_override_seconds
        self.store = OffsetStore(state_path)
        self.lock = threading.RLock()  # the MT5 bridge is not thread-safe
        self.initialized = False
        self.last_init_ms = None  # type: Optional[int]
        self.last_detect_ms = None  # type: Optional[int]
        self.market_open_hint = None  # type: Optional[bool]
        self.last_tick_time = {}  # type: Dict[str, int]
        self.probe_symbol = None  # type: Optional[str]
        self._initialize()

    # -------------------------------------------------------------- time and terminal

    def now_ms(self) -> int:
        return int(self.clock() * 1000)

    def _initialize(self) -> None:
        self.last_init_ms = self.now_ms()
        try:
            self.initialized = bool(self.mt5.initialize())  # no credentials: the owner signs the terminal in
        except Exception:
            self.initialized = False

    def _terminal(self) -> None:
        """Make sure the terminal answers; a failed initialization is retried (still without credentials)."""
        if not self.initialized and self.now_ms() - (self.last_init_ms or 0) >= REINIT_INTERVAL_MS:
            self._initialize()
        if not self.initialized:
            raise HttpError(503, "terminal_unavailable")

    def terminal_connected(self) -> bool:
        if not self.initialized:
            return False
        try:
            info = self.mt5.terminal_info()
        except Exception:
            return False
        return bool(_get(info, "connected")) if info is not None else False

    # -------------------------------------------------------------- offset

    def _fresh_observation(self, now_ms: int) -> Optional[Dict[str, int]]:
        latest = self.store.latest()
        if latest is not None and now_ms - latest["observed_at_ms"] <= OFFSET_FRESH_MS:
            return latest
        return None

    def offset_known(self) -> bool:
        return self.override is not None or self._fresh_observation(self.now_ms()) is not None

    def detect(self, symbol: Optional[str] = None) -> Optional[int]:
        """One detection attempt from the symbol's latest tick; records an observation when it is trustworthy.

        Trustworthy = the residual rule (``rounded_offset``) AND the tick time changed since the previous
        reading of the symbol. Returns the detected offset or ``None``."""
        now_s = self.now_ms() / 1000.0
        names = [symbol] if symbol else self._probe_names()
        for name in names:
            tick = self.mt5.symbol_info_tick(name)
            tick_time = _int(_get(tick, "time")) if tick is not None else None
            if tick_time is None or tick_time <= 0:
                continue
            previous = self.last_tick_time.get(name)
            self.last_tick_time[name] = tick_time
            offset = rounded_offset(tick_time - now_s)
            if offset is None or previous is None or tick_time == previous:
                continue
            self.probe_symbol = name
            self.store.add(self.now_ms(), offset)
            return offset
        return None

    def _probe_names(self) -> List[str]:
        names = [self.probe_symbol] if self.probe_symbol else []
        if not names:
            try:
                found = self.mt5.symbols_get() or []
            except Exception:
                found = []
            names = [str(_get(item, "name")) for item in list(found)[:3] if _get(item, "name")]
        return names

    def maybe_detect(self, symbol: Optional[str] = None) -> None:
        """Detect at most once a minute (and not at all with an override)."""
        if self.override is not None:
            return
        now = self.now_ms()
        if self.last_detect_ms is not None and now - self.last_detect_ms < DETECT_INTERVAL_MS:
            return
        self.last_detect_ms = now
        try:
            self.market_open_hint = self.detect(symbol) is not None
        except Exception:
            self.market_open_hint = False

    def resolve_offset(self, from_ms: int, to_ms: int) -> Tuple[int, str]:
        """The offset to convert broker time in ``[from_ms, to_ms]`` to UTC, with its method; never guesses."""
        if self.override is not None:
            return self.override, "override"
        seen = {item["offset_seconds"] for item in self.store.within(from_ms, to_ms)}
        if len(seen) > 1:
            raise HttpError(409, "offset_changed")
        if not seen:
            raise HttpError(503, "offset_unknown")
        return seen.pop(), "detected"

    # -------------------------------------------------------------- routes

    def health(self) -> Dict[str, Any]:
        connected = self.terminal_connected()
        return {"ok": bool(self.initialized), "terminal_connected": connected,
                "offset_known": self.offset_known(), "now_utc_ms": self.now_ms()}

    def clock_report(self) -> Dict[str, Any]:
        with self.lock:
            self._terminal()
            self.maybe_detect()
            now = self.now_ms()
            if self.override is not None:
                offset, method, observed = self.override, "override", None
            else:
                latest = self._fresh_observation(now)
                offset = latest["offset_seconds"] if latest else None
                method = "detected" if latest else None
                observed = latest["observed_at_ms"] if latest else None
            return {"now_utc_ms": now, "server_offset_seconds": offset, "method": method, "observed_at_ms": observed,
                    "market_open_hint": self.market_open_hint}

    def symbols(self) -> Dict[str, Any]:
        with self.lock:
            self._terminal()
            found = self.mt5.symbols_get()
            if found is None:
                raise HttpError(502, "mt5_error")
            return {"symbols": sorted({str(_get(item, "name")) for item in found if _get(item, "name")})}

    def specs(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        raw = (query.get("symbols") or [""])[0]
        names = [name for name in raw.split(",") if name]
        if not names or len(names) > MAX_SPEC_SYMBOLS or len(set(names)) != len(names) \
                or not all(SYMBOL_PATTERN.match(name) for name in names):
            raise HttpError(400, "bad_symbols")
        with self.lock:
            self._terminal()
            out = {}  # type: Dict[str, Any]
            for name in names:
                info = self.mt5.symbol_info(name)
                if info is None:
                    out[name] = None
                    continue
                spec = {}  # type: Dict[str, Any]
                for field in SPEC_FIELDS:
                    value = _get(info, field)
                    if field in INT_SPEC_FIELDS:
                        spec[field] = _int(value)
                    elif field in FLOAT_SPEC_FIELDS:
                        spec[field] = _float(value)
                    else:
                        spec[field] = None if value is None else str(value)
                out[name] = spec
            return {"specs": out}

    def bars(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        symbol = (query.get("symbol") or [""])[0]
        raw_from, raw_to = (query.get("from_ms") or [""])[0], (query.get("to_ms") or [""])[0]
        if not SYMBOL_PATTERN.match(symbol) or not INT_PATTERN.match(raw_from) or not INT_PATTERN.match(raw_to):
            raise HttpError(400, "bad_request")
        from_ms, to_ms = int(raw_from), int(raw_to)
        if from_ms >= to_ms:
            raise HttpError(400, "bad_request")
        if to_ms - from_ms > MAX_RANGE_MS:
            raise HttpError(400, "range_too_large")
        with self.lock:
            self._terminal()
            if not self.mt5.symbol_select(symbol, True):  # Market Watch selection only
                raise HttpError(404, "unknown_symbol")
            self.maybe_detect(symbol)
            offset, method = self.resolve_offset(from_ms, to_ms)
            now = self.now_ms()
            first_s = from_ms // 1000 + offset
            last_s = -(-to_ms // 1000) + offset
            rates = self.mt5.copy_rates_range(symbol, self.mt5.TIMEFRAME_M1, first_s, last_s)
            if rates is None:
                raise HttpError(502, "mt5_error")
            rows = {}
            for rate in rates:
                open_ms = (int(_get(rate, "time")) - offset) * 1000
                # [from, to) only, and only minutes that are fully closed in UTC.
                if not (from_ms <= open_ms < to_ms) or open_ms + MINUTE_MS > now:
                    continue
                rows[open_ms] = {
                    "time_basis": "utc", "native_symbol": symbol, "open_time_ms": open_ms,
                    "open": _float(_get(rate, "open")), "high": _float(_get(rate, "high")),
                    "low": _float(_get(rate, "low")), "close": _float(_get(rate, "close")),
                    "spread_points": int(_get(rate, "spread")), "tick_volume": int(_get(rate, "tick_volume"))}
            ordered = [rows[key] for key in sorted(rows)]
            if any(row[name] is None for row in ordered for name in ("open", "high", "low", "close")):
                raise HttpError(502, "mt5_error")
            return {"symbol": symbol, "offset_seconds": offset, "offset_method": method, "bars": ordered}

    # -------------------------------------------------------------- dispatch

    def authorized(self, authorization: Optional[str]) -> bool:
        if not self.token or not authorization or not authorization.startswith("Bearer "):
            return False
        presented = authorization[len("Bearer "):].encode("utf-8")
        return hmac.compare_digest(presented, self.token.encode("utf-8"))

    def handle(self, method: str, target: str, authorization: Optional[str]) -> Tuple[int, Dict[str, Any]]:
        """(status, JSON body) for one request. Pure with respect to sockets; never raises."""
        try:
            if len(target) > MAX_TARGET_LENGTH:
                return 414, {"error": "uri_too_long"}
            parts = urlsplit(target)
            if len(parts.query) > MAX_QUERY_LENGTH:
                return 414, {"error": "uri_too_long"}
            if method != "GET":
                return 405, {"error": "method_not_allowed"}
            if parts.path == "/health":
                return 200, self.health()
            if not self.authorized(authorization):
                return 401, {"error": "unauthorized"}
            query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=20)
            if parts.path == "/v1/clock":
                return 200, self.clock_report()
            if parts.path == "/v1/symbols":
                return 200, self.symbols()
            if parts.path == "/v1/specs":
                return 200, self.specs(query)
            if parts.path == "/v1/bars":
                return 200, self.bars(query)
            return 404, {"error": "not_found"}
        except HttpError as error:
            return error.status, {"error": error.code}
        except ValueError:
            return 400, {"error": "bad_request"}
        except Exception:
            return 500, {"error": "internal_error"}


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10  # socket timeout: one silent client must not block the single-threaded server
        server_version = "mt5-gateway"
        sys_version = ""

        def _respond(self) -> None:
            status, body = gateway.handle(self.command, self.path, self.headers.get("Authorization"))
            try:
                payload = json.dumps(body, allow_nan=False).encode("utf-8")
            except ValueError:
                status, payload = 500, b'{"error": "internal_error"}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if status == 401:
                self.send_header("WWW-Authenticate", "Bearer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _respond

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # Method, path without the query, status. Headers (the token) are never logged.
            sys.stderr.write("%s %s\n" % (self.command, urlsplit(self.path).path[:100]))

    return Handler


def load_mt5(backend: str, host: str, port: int) -> Any:
    if backend == "mt5linux":
        from mt5linux import MetaTrader5  # type: ignore
        return MetaTrader5(host=host, port=port)
    import MetaTrader5  # type: ignore
    return MetaTrader5


def make_server(bind: str, port: int, gateway: Gateway) -> HTTPServer:
    """A single-threaded server: the MT5 bridge may only be called from the thread that initialized it."""
    return HTTPServer((bind, port), make_handler(gateway))


def main(argv: Optional[List[str]] = None, environ: Optional[Dict[str, str]] = None) -> int:
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Read-only MT5 market-data gateway")
    parser.add_argument("--backend", choices=("mt5", "mt5linux"), default="mt5")
    parser.add_argument("--mt5-host", default="127.0.0.1")
    parser.add_argument("--mt5-port", type=int, default=8001)
    parser.add_argument("--state-path", default="mt5_gateway_state.json")
    args = parser.parse_args(argv)
    token = environ.get("MT5_GATEWAY_TOKEN", "")
    if not TOKEN_PATTERN.match(token):
        sys.stderr.write("MT5_GATEWAY_TOKEN is required and must match ^[A-Za-z0-9_-]{32,256}$\n")
        return 2
    override = None
    if environ.get("MT5_SERVER_UTC_OFFSET_SECONDS"):
        try:
            override = int(environ["MT5_SERVER_UTC_OFFSET_SECONDS"])
        except ValueError:
            sys.stderr.write("MT5_SERVER_UTC_OFFSET_SECONDS must be an integer\n")
            return 2
    bind = environ.get("MT5_GATEWAY_BIND", "127.0.0.1")
    try:
        port = int(environ.get("MT5_GATEWAY_PORT", "8002"))
    except ValueError:
        sys.stderr.write("MT5_GATEWAY_PORT must be an integer\n")
        return 2
    if bind in ("0.0.0.0", "::"):
        sys.stderr.write("warning: binding to every interface; use the VM's host-only address instead\n")
    try:
        gateway = Gateway(load_mt5(args.backend, args.mt5_host, args.mt5_port), state_path=args.state_path,
                          token=token, offset_override_seconds=override)
    except ValueError as error:
        sys.stderr.write("%s\n" % error)
        return 2
    server = make_server(bind, port, gateway)
    sys.stderr.write("mt5-gateway listening on %s:%d (terminal initialized: %s)\n"
                     % (bind, server.server_address[1], gateway.initialized))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
