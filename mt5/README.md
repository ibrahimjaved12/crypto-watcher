# MT5 spike (#393)

Tests whether Exness demo candles reach Python on Linux through MetaTrader 5 under Wine
(`gmag11/metatrader5_vnc`). This is a spike for #393; the Linux/Wine setup for the long-running
gateway is decided after it.

1. Prerequisites: Docker, and an Exness demo MT5 account (login number, trading password, server
   name). Never commit any of them.
2. Start it:
   ```sh
   cd mt5
   cp .env.example .env   # then set MT5_VNC_PASSWORD
   docker compose up -d   # the first start downloads about 4 GB
   ```
3. Open `http://localhost:3000` and sign in with the browser-screen user and password from `.env`.
   In MT5 use File > Login to Trade Account with the demo details, then right-click Market Watch >
   Show All.
4. Run the check from the repo root and keep the output:
   ```sh
   pip install mt5linux
   python mt5/spike_check.py
   ```
5. Forex is closed from Friday evening to Sunday evening UTC: prices do not move then, and the
   clock check needs the market open.
6. Stop with `docker compose down` (the `config` folder keeps the login). Both ports are bound to
   loopback only; never publish 8001, which allows code execution inside the container.

## Gateway (#452)

`mt5/gateway/mt5_gateway.py` is a small read-only HTTP service that runs next to the MetaTrader 5
terminal and serves completed 1-minute BID bars, symbol specs and the clock. The forex collector (#453)
on the host is its only client. It initializes the terminal without credentials, never trades and never
reads account data. One file, standard library plus the MT5 module, Python 3.9.

**The terminal must be signed in by the owner, by hand.** The gateway never sends a login, never
retries with credentials, and shows no account, server or balance data in `/health`.

### Run it in a Windows VM (official package)

```bat
py -m pip install MetaTrader5
set MT5_GATEWAY_TOKEN=<32 to 256 characters of A-Z a-z 0-9 _ ->
set MT5_GATEWAY_BIND=<the VM's host-only address>
set MT5_GATEWAY_PORT=8002
py mt5_gateway.py --state-path C:\mt5-gateway\offsets.json
```

Start the MT5 terminal first and sign it in. `MT5_SERVER_UTC_OFFSET_SECONDS` is optional (see below).

### Run it beside the Linux/Wine bridge

With the spike container from above running (its `mt5linux` bridge on loopback port 8001):

```sh
pip install mt5linux
MT5_GATEWAY_TOKEN=... python3 mt5/gateway/mt5_gateway.py --backend mt5linux --mt5-host 127.0.0.1 --mt5-port 8001
```

### Network and token

- Bind to the VM's **host-only** network address. Never `0.0.0.0` on a bridged adapter: the gateway
  serves market data only, but the token is the only protection. The default bind is `127.0.0.1`.
- The token lives only in the environment of the gateway and in the collector's host configuration
  (`.env.production.local` there). It is never committed and the gateway never logs it. Every route
  except `GET /health` needs `Authorization: Bearer <token>`; there are no CORS headers.

### Routes

| Route | Answer |
| --- | --- |
| `GET /health` | `{ok, terminal_connected, offset_known, now_utc_ms}` (no token needed) |
| `GET /v1/clock` | `{now_utc_ms, server_offset_seconds, method: "override"/"detected"/null, observed_at_ms, market_open_hint}` |
| `GET /v1/symbols` | `{symbols: [names]}` |
| `GET /v1/specs?symbols=A,B` | `{specs: {A: {...}, B: null}}`, `null` for an unknown name |
| `GET /v1/bars?symbol=N&from_ms=..&to_ms=..` | completed minutes with open time in `[from_ms, to_ms)`, at most 5000 per request |

Bar rows are `{time_basis: "utc", native_symbol, open_time_ms, open, high, low, close, spread_points,
tick_volume}`; the collector adds `venue` and `transport` before `fx_bars_from_rows`. A minute is served
only when it is fully closed in UTC. Errors are `{"error": "<code>"}`: `400 bad_request|range_too_large|
bad_symbols`, `401 unauthorized`, `404 unknown_symbol|not_found`, `409 offset_changed`, `502 mt5_error`,
`503 offset_unknown|terminal_unavailable`.

Spec fields (MT5 names): `digits`, `point`, `trade_tick_size`, `trade_tick_value`, `trade_contract_size`,
`volume_min`, `volume_max`, `volume_step`, `currency_base`, `currency_profit`, `currency_margin`,
`trade_mode`, `swap_mode`, `swap_long`, `swap_short`, `swap_rollover3days`. The collector maps them to the
spec store's names (`tick_size`, `tick_value`, `contract_size`, `base_currency`, `profit_currency`,
`margin_currency`) and adds `venue`, `instrument_id` and **`account_currency`**: the account currency is
account data, so the gateway does not read it; the collector must take it from its own configuration (and
`trade_tick_value` is in that currency).

### Broker clock offset

MT5 returns "server time" as plain numbers; converting to UTC needs the broker server's offset, which is
**never guessed**:

1. `MT5_SERVER_UTC_OFFSET_SECONDS` (a multiple of 900, within +-14 hours) is used as is. With an override
   the gateway does not detect, so a daylight-saving change at the broker is **not noticed**.
2. Without it the gateway detects from a symbol's latest tick: `tick.time - now_utc` rounded to a multiple
   of 900 s must leave a residual of at most 60 s, and the tick time must have changed since the previous
   reading (a stale tick can pass the residual rule by chance). At most one detection a minute, so the first
   observation needs two readings a minute apart while the market is open. Each observation
   `{observed_at_ms, offset_seconds}` goes to the JSON state file (written atomically; the newest 2000 are
   kept).
3. A bars request needs an offset for its range: all observations inside `[from_ms, to_ms]` must agree,
   otherwise `409 offset_changed`; none and no override is `503 offset_unknown`. So the collector reads
   recent minutes while detection runs, not far-past history.

**The offset can change twice a year** (broker daylight saving time). Check it after each change.

### UNVERIFIED broker and package behaviour

- That `copy_rates_range` accepts integer broker-time seconds (the package documents datetime objects or
  seconds), that it includes or excludes the `to` instant (the gateway filters to `[from, to)` itself), and
  whether it returns `None` or an empty array for a closed-market range (`None` is answered `502 mt5_error`).
- That `symbol_info` answers for symbols that are not in Market Watch (`null` is returned for them).
- That the server's `tick.time` is broker server time as plain seconds, and that the bar `time` is the bar
  open in the same broker time.
- That `terminal_info().connected` means "connected to the broker's trade server".
- That a quarter-hour grid is right for the Exness demo server (its offset is not known here).
- The `mt5linux` backend's object types (rates may arrive as plain tuples or numpy rows).
