# Alert setup charts (#299, #298)

Owner decision 2026-10-16: every movement alert and every TA pattern event gets a setup chart, made
and stored on the owner's machine now. Delivery (email/WhatsApp) stays parked; it will attach the
stored PNG later. The site viewer stays in #298.

## Flow

```text
movement alert saved (process_cumulative_observation -> "alerted")   TA rows written with patterns
        \                                                               /
         captureMovementAlertChart / capturePatternCharts (TanStack, best effort)
                                  |
          operational DB: enqueue_alert_chart  -> alert_charts row (status pending)
                                  |
          worker-host child `alert-charts` (claim with lease, one at a time)
                                  |
          python -m market_analysis_tools.alert_charts render  -> PNG under ALERT_CHART_DIR
                                  |
          finish_alert_chart: rendered (path, sha256, bytes) | failed (error)
```

**Why the operational database is the queue.** Capture needs the operational store anyway (the 15m
window comes from the collector's candles and the breadth from `market_state_current`), and the
operational store is only enabled where it is reachable, which in production is the machine. The row
that stores the window is therefore also the job; nothing crosses to hosted Supabase. The hosted
`runner_jobs` queue (#294) was not reused: it is per user, allows one active job per user and kind,
and lives in hosted main, while the windows and PNGs belong on the machine. Its rules are reused:
120 s lease, an elapsed lease becomes `failed` ("Runner restarted before finishing"), pending rows
older than the expiry (default 15 minutes, `ALERT_CHART_PENDING_EXPIRY_MINUTES`) become `expired`, and
nothing is replayed automatically.

Where capture does **not** run: a monitor or TA run without the operational store (the Cloudflare
Worker, or a local setup with `OPERATIONAL_DB_ENABLED=false`) skips capture. In production the
Worker's scheduled monitor is off and checks run on the machine (runner gateway), so alerts created
there are captured.

## Windows (point-in-time)

Stored compactly in `alert_charts.candle_window` (`alert-chart-window-v1`), candles as
`[open_ms, open, high, low, close, volume]`, oldest first. Nothing after the event candle: the
capture filters it and `enqueue_alert_chart` refuses a window with a later candle.

- **Movement alert.** The last 96 completed 15m candles (24 h) that closed at or before the alert
  observation (the developing 15m candle containing the alert minute is never included), the
  completed 1m alert candle itself, baseline price and time, alert price and time, direction,
  threshold, change, and the market breadth of the movement engine's primary 5m window when its state
  is LIVE and its evaluation boundary is within 2 minutes of the alert (otherwise the reason it is
  unavailable; the engine keeps no breadth history, so a reading from another time is never
  borrowed). EMA 20/50 and RSI 14 are computed over every completed 15m candle read (up to 260), so
  EMA 50 is warmed up before the window starts. Identity: `(movement_alert, symbol, 1, alert candle
  open, cumulative-baseline-v1, user id)`; baselines and thresholds are per account.
- **Pattern event.** About 60 completed candles of the event timeframe ending at the event candle,
  the pattern names (all patterns of that candle in one chart), the TA version as rule version, and
  EMA 20/50 and RSI 14 for the window, computed over the same point-in-time prefix the TA request used
  (the TypeScript port in `src/lib/alert-charts/capture.ts` matches `technical.py` `_ema`/`_rsi`; a
  Node test compares them against the Python functions). Identity: `(pattern, symbol, timeframe, event
  candle open, ta version, '')`, market-wide, so several accounts produce one chart. Only rows inserted
  by this run are captured.

A capture failure (exchange or store error, cap reached) is logged as `[alert-charts] ...` and never
fails or rolls back the alert or the TA signal.

## Rendering

`python/market_analysis_tools/alert_charts.py` (outside the standard-library calculation core
`market_analysis`; matplotlib is in `python/requirements-machine.txt` only):

- `render_alert_chart(window, markers, path)`: candles, EMA 20/50, volume, RSI 14, baseline line,
  threshold line, alert price line, the 1m alert candle drawn after the window and highlighted, the
  breadth reading;
- `render_pattern_snapshot(window, markers, path)`: candles, EMA 20/50, volume, RSI 14, event candle
  highlighted and annotated with the pattern names;
- `render_trade_overlay(window, trade, path)` and `render_equity(series, path)`: the other two #299
  reference charts, ready for paper-trading use; not wired to any job yet.

The look is the owner-approved #299 reference (dark palette, same panel layout). The output is
deterministic for the same input on the same matplotlib/FreeType build (no time metadata in the PNG).
PNG files: `ALERT_CHART_DIR/<kind>/<SYMBOL>/<YYYY-MM-DD>/<chart_id>.png`, written to a temporary name
and renamed. The stored window can regenerate any PNG.

## Operation

```sh
python/.venv/bin/python -m pip install -r python/requirements-machine.txt   # once, on the machine
# .env.production.local
ALERT_CHART_DIR=/absolute/path/for/alert-charts
npm run worker:host                                                          # starts child `alert-charts`
npm run alert-charts -- --mode production --event-id <alerts.id or ta_signals.id>   # re-render one event
npm run mvp:check -- --mode production                                       # line "alert charts"
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `ALERT_CHARTS_ENABLED` | `true` | `false` stops capture (the child does not start) |
| `ALERT_CHART_DIR` | unset | absolute PNG directory; without it the child does not start and jobs wait, then expire |
| `ALERT_CHART_MAX_ROWS` | 100000 | row cap |
| `ALERT_CHART_MAX_BYTES` | 5 GiB | cap on stored window bytes plus recorded PNG bytes |
| `ALERT_CHART_PENDING_EXPIRY_MINUTES` | 15 | pending rows older than this expire (runner-job rule) |
| `ALERT_CHART_PYTHON` | `python/.venv/bin/python` | interpreter with matplotlib |

**Retention.** At a cap new captures are refused (the alert itself is saved) and `mvp:check` shows
FAIL; at 80 % it shows WARN. Nothing is deleted automatically. To free space, the owner archives or
deletes old PNG files and their rows deliberately (SQL on the operational database).

**Offline machine.** If the whole machine is off, no alert is created there, so there is nothing to
capture. If only the renderer is down, rows wait and expire after the expiry window; re-run with
`--event-id`. A longer expiry can be set with `ALERT_CHART_PENDING_EXPIRY_MINUTES`.

## Not done here

- No site change: the site reads hosted data only, and the chart lives on the machine. The viewer and
  any "chart available" flag belong to #298.
- No delivery (email/WhatsApp), parked.
- Trade overlay and equity charts are not produced automatically yet.
