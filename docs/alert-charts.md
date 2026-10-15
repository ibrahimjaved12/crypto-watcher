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
older than the expiry become `expired`, and nothing is replayed automatically. **The runner-job expiry
(15 minutes) is intentionally relaxed for this queue:** the default is 7 days
(`ALERT_CHART_PENDING_EXPIRY_MINUTES=10080`, the maximum) because the owner's intent is "if the machine is
offline, jobs wait", and the stored window means a long wait loses nothing.

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

### Capture failures, retry and timing

A capture never fails or rolls back the alert or the TA signal, and a movement capture never delays the
next symbols: it is **started without awaiting** inside the symbol loop and all captures of a run are
awaited together after every alert is saved (and after TA, which runs meanwhile), for at most **10 s**.
A capture still running at the cap is abandoned (it reads and enqueues nothing from then on), logged by
symbol, and recorded as a failure.

- **Retry.** A read step (the alert row, the 15m candles) is retried **once after about 2 s**. Nothing
  else is retried: an alert without a baseline, a window that fails validation, a cap refusal and the
  enqueue write are not transient. Pattern captures have no read step (the candles are already in
  memory) and are not retried.
- **Limits.** A capture that fails after that retry is **not retried again automatically**; the 8-day
  candle store means its window cannot be rebuilt later, so the chart is lost. A capture abandoned
  at the cap while its enqueue write was already in flight may still create the chart and also be
  recorded as a failure.
- **Reporting.** Each failed or abandoned capture is written to `alert_chart_capture_failures` (event
  kind and id, symbol, timeframe, event candle, error up to 500 characters; idempotent on kind, symbol,
  timeframe and candle; recording is best effort and never throws). `mvp:check` prints the count in
  the last 24 hours and in total on the `alert charts` line: WARN when any exist in the last 24 hours,
  never FAIL by itself (FAIL stays reserved for a reached cap).

## Rendering

`python/market_analysis_tools/alert_charts.py` (outside the standard-library calculation core
`market_analysis`; matplotlib is in `python/requirements-machine.txt` only):

- `render_alert_chart(window, markers, path)`: candles, EMA 20/50, volume, RSI 14, the baseline line,
  the threshold line (drawn, not labelled), the alert price line with **one merged label** ("Alert
  73,198.25 (+1.51%, threshold 1.5%)"; alert and threshold are almost equal by construction), and the
  market breadth reading as a subtitle under the title. Labels sit in a reserved right margin; the
  baseline and alert labels are kept at least 4 % of the visible price span apart (`spread_labels`, a
  pure function with a test), with a short leader line to the true level. The completed 1m alert candle
  is drawn **one empty slot after** the last 15m candle behind a dotted break, with the note "alert N min
  after the last completed 15m candle closed (candles are not adjacent)", N computed from the window
  (it can be up to 14 minutes);
- `render_pattern_snapshot(window, markers, path)`: candles, EMA 20/50, volume, RSI 14, event candle
  highlighted and annotated with the pattern names;
- `render_trade_overlay(window, trade, path)` (level labels in the right margin with the same minimum gap) and
  `render_equity(series, path)`: the other two #299 reference charts, ready for paper-trading use; not
  wired to any job yet.

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
| `ALERT_CHART_PENDING_EXPIRY_MINUTES` | 10080 | pending rows older than this expire (1-10080; 7 days; the runner-job 15 minutes is relaxed on purpose) |
| `ALERT_CHART_PYTHON` | `python/.venv/bin/python` | interpreter with matplotlib |

**Retention.** At a cap new captures are refused (the alert itself is saved) and `mvp:check` shows
FAIL; at 80 % it shows WARN. Nothing is deleted automatically. To free space, the owner archives or
deletes old PNG files and their rows deliberately (SQL on the operational database).

**Offline machine.** If the whole machine is off, no alert is created there, so there is nothing to
capture. If only the renderer is down, rows wait up to 7 days (then expire; re-run with `--event-id`).

**Re-running one event.** `--event-id` first sweeps expired leases (a `rendering` row whose lease ran out
becomes `failed`), then requeues every non-rendering chart of the event and renders it. Messages:
`No chart for this event id.` versus `Chart <id> is rendering right now (lease until HH:MM UTC)`. A
pattern chart is market-wide and stored under the first account's `ta_signals.id`; every other signal
id for that candle is mapped in `alert_chart_event_ids` on each capture attempt (also for duplicates),
so `--event-id` accepts any of them.

## Not done here

- No site change: the site reads hosted data only, and the chart lives on the machine. The viewer and
  any "chart available" flag belong to #298.
- No delivery (email/WhatsApp), parked.
- Trade overlay and equity charts are not produced automatically yet.
- A window whose capture failed is not rebuilt later (see "Capture failures" above).
