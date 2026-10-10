# Forward harness (signals, setups, outcomes, paper wallet)

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Tracking issue:
> [#239](https://github.com/ibrahimjaved12/crypto-watcher/issues/239) (refs #30, #31, #32, #33,
> #36, #68).

**No strategy has a validated edge. This is a forward test: it records what the registered
strategies and their placebo controls would have done, point in time, with realistic costs.**

## Shape

Python stays calculation-only (`docs/operational-database.md`). The package
`python/market_analysis/forward/` is pure and stateless: no network, no database, no wall clock.
`POST /v1/forward/evaluate` returns everything new, and the TanStack application validates and
persists it (`forward_*`, `paper_*` tables).

| Module            | Does                                                                                                                                             |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `bars_adapter.py` | Collector 1-minute rows (doubles) become a benchmark `BarSeries` (integers at 10^8). Also builds 15/60/240 candles with `candles.build_candles`. |
| `signals.py`      | The `FORWARD_STRATEGIES` registry and `generate_signals`.                                                                                        |
| `setups.py`       | `build_setup`, using the labels-v3 `ewma-robust-hcal` geometry (P16; `ForwardSigma`).                                                           |
| `outcomes.py`     | `resolve_setup`: incremental resolution, identical to `labels.build_labels`.                                                                     |
| `wallet.py`       | `step(state, events, config)`: the paper wallet, pure and append-only.                                                                           |
| `evaluate.py`     | One evaluation; the body of the endpoint.                                                                                                        |

## What is recorded

- **Signal** (`forward_signals`):
  - Fields: strategy id and version, symbol, decision time `signal_ms`, side, and horizon.
  - Id: `signal_id = sha256(strategy_id|version|symbol|signal_ms|side)`, so re-running is
    idempotent.
- **Setup** (`forward_setups`, immutable): one per `(k, rr)` of the grid. With `k = 2` and
  `rr in {1.5, 2}` that is two setups per signal, each with its own id; K is visible.
  - It saves every input: the robust level (`var`), the horizon multiplier c_h
    (`factor_weight`, fixed point 10^6), sigma, the entry open `p0`, tick, `d_ticks`, stop and
    target prices, the label leverage, the time limit, the label parameter hash (`params_hash`) and
    every component version.
  - Its status is T for tradeable, or one of the non-trade codes of the labels: C, P, G, N.
  - Both signal and setup rows carry `candle_version` (P15): SHA-256 over the candle-v1 hashes of
    the 1m candles forming the decision candle (operational `collector_candle_hash`). A later REST
    revision of one of those candles (`collector_candle_revisions.supersedes`) can be traced to it.
- **Outcome** (`forward_outcomes`, append-only): `pending | open | T | S | E | L | X | ambiguous`.
  - The net, cost and funding are in micro-R (1 R = the entry-to-stop distance).
  - For `ambiguous`, the pessimistic result is the net. The optimistic one is stored beside it
    and never chosen.
- **Paper ledger** (`paper_ledger`, append-only): open fees, PnL, close fees, funding,
  liquidations and rejections.
  - Each line carries the running balance, a sequence number and assumption codes.
  - `initial balance + sum(amounts) == balance` holds exactly.

## Point-in-time rules

- **Signals:** a TA signal at a candle close uses closed candles only (`ta-v1`). The entry is
  the open of the minute after the decision. A decision is evaluated only once that entry minute
  exists in the data (`processed_to_ms`).
- **Sigma (P16, 2026-10-10):** `ewma-robust-hcal` (labels-v3, the lb3h release model), chosen
  because the calibration audit found it closest to theory (target-first shares match b/(a+b) and
  the k = 1 expiry share matches the continuous-monitoring theory).
  - Robust |r| level (EWMA of |5-minute return| / 0.7979) of the block ending at the decision, the
    entry day's |r| slot factors (28-day window, days before D only), times the point-in-time
    horizon multiplier c_h (`volatility.horizon_calibration` on the label-step grid: median of
    |ln(open[e+h]/open[e])| / sigma over completed past windows, / 0.6745, clipped to [0.5, 2]).
  - c_h exists only after 60 days of completed past windows, so the job reads about 120 days of 1m
    history (`FORWARD_HISTORY_DAYS = 120`, operational 1m retention 140 days). Hourly job runs
    (never "Run now") repair it from public REST klines: the range before the first stored minute
    (from 130 days back) and every interior gap, at most 50 ranges per symbol per run. The ranges
    are derived from what is stored, so a backfill interrupted by 418/429 or a timeout leaves a
    hole that the next hourly run sees and fills. The failure is recorded in the run's reason
    (`backfill_failed: SYMBOL: ...`). A minute Binance never published stays a gap (one page per
    run) and reaches Python as MISSING. The first backfill (6 symbols x about 125 pages) runs
    inside one job call and can take minutes; run `forward-run --once` once before relying on it.
  - Until the sigma exists a signal emits **no setup** and the evaluation reports
    `reasons[SYMBOL].sigma = "no_sigma: ..."`; it never falls back to another model.
  - **Known difference from the benchmark:** over the rolling 120-day request c_h is an expanding
    median from the request start (point in time, but only about 30 to 60 days of windows), while
    the lb3h labels expand from 2024-01. Forward setups therefore do not use exactly the same sigma
    as the benchmark. The cleaner fix (not done) is seeding c_h from the lake.
  - **Request size:** each run sends the whole window (up to 120 x 1,440 x 6, about 1 million rows,
    roughly 100 MB of JSON). Fine for the local MVP, not for free-tier hosting. Each ok run records
    `freshness.request = {rows, python_ms}`; if it is slow, send only the recent window plus a
    persisted per-symbol sigma state.
  - Half-lives come from `LabelParams`: 1 day at 15 m, 3 days at 60 m, 7 days at 240 m.
- **Resolution:** a resolution uses only minutes from the entry onward and is final once an event
  occurs. Calling it again with more bars never changes a final result.
- **Placebo control (`placebo-v1`):**
  - There is one placebo per TA strategy and timeframe.
  - At every hourly decision it emits with probability equal to the matched strategy's signal
    rate. The side comes from an independent draw by
    `rng.u64_words(seed, "forward-placebo:SYMBOL:MATCHED", 2 * minute, 2)`, so it is
    reproducible and stateless.
  - The rate is the registry's frozen `historical_rate` once it is set from the development
    run. Until then it is the matched strategy's point-in-time rate over the trailing 30 days,
    and responses say so (`rate:trailing-30d`).
- **Insufficient history:** a strategy without enough history returns a reason code and no
  signal. Nothing is filled.
- **Staleness:** the orchestration skips a run (`skipped: stale`) when the last 48 hours of candles
  (`STALE_GAP_WINDOW_MS`) have a gap, the last completed minute is missing, or the collector is not
  LIVE. Older gaps do not block the engine (a laptop sleep would otherwise block it for the whole
  120-day window): those minutes reach Python as missing bars, which the sigma and labels treat as
  MISSING exactly as in the benchmark. `freshness[SYMBOL].missing_minutes` counts them.

## Comparison with backtests

Forward resolution _is_ the benchmark label. A test builds labels for a synthetic fixture with
`build_labels` and checks that every forward setup and resolution equals the label row field by
field: status, p0, sigma, d_ticks, leverage, outcome, net/cost/funding micro-R and ambiguity
(the geometry test runs on the labels-v2 parameters; a second test checks that the hcal sigma is
exactly `RobustSigma(hcal=True).sigma` and that nothing is emitted before 60 days of windows).

So a forward outcome table by strategy and version is directly comparable with the
development and validation reports for the same strategy id, version and parameter hash. The
differences are only in the data:

- live collector candles against the lake's aggTrade bars;
- mark prices are a proxy (see below).

The paper wallet is a separate, money-denominated view. It sizes 1 R as 1% of equity and uses
taker fees on both sides, so its PnL is not the label's micro-R.

## Assumptions

1. **Mark proxy:** the collector stores no mark price, so mark OHLC equals trade OHLC.
   Liquidation checks and funding settlement marks use trade prices.
2. **No index, premium, taker-buy volume or trade count** in collector rows. Order-flow
   strategies cannot run live yet.
3. **Funding** is applied only when the caller supplies funding events (`symbols[].funding`).
   Without them the funding amounts are zero.
4. **Flat maintenance-margin tier:** no symbol has a tier table in the code yet, so a flat
   `COST_MODEL_V1.mmr` (1%) is used. Every ledger line carries `flat-tier`.
5. **Program-chosen leverage:** the largest integer leverage (cap 20) whose liquidation price is
   at least 2x the stop distance beyond the entry. The labels use 3x for their own leverage.
6. **Wallet costs:** taker fee and a 1 bp slippage floor on both entry and exit, including
   target exits. This is more conservative than the label's maker target.
7. **X exits:** an X resolution (data compromised) closes the paper position at its entry fill.
   Only fees are charged.
8. **Sizing and caps use the realized balance;** open positions are not marked to market.
   Defaults: 100 USDT, 1% risk, at most 6 positions, total notional at most 5x the balance.
9. **Placebo rates** are the trailing-30-day point-in-time rates until frozen constants are set.
10. **Registry size:** it holds all six `ta-v1` strategies of `ta_strategies.STRATEGIES`. The
    MVP prompt said five; the code has six, so six are registered.
11. **Request coverage:** saved setups whose entry precedes the request's first bar are skipped.
    The caller must send bars from the earliest open setup's entry.

## Daily trend track (portfolio mode, #239 P14, #222)

The nine frozen daily trend Mode B variants are hypothetical vol-targeted portfolios, with the
three vol-targeted buy-and-hold controls and `ew_long`. There is no margin, liquidation or
position-limit model. No variant has a validated edge.

### Canonical history and startup

The operational `forward_daily_bars` feed stores completed UTC-day Binance USD-M klines from
**2020-01-01**, matching `benchmark.trend.WARMUP_FIRST_MONTH`. Initial requests paginate the
public `/fapi/v1/klines?interval=1d` endpoint (1500 rows per page) until the last completed day.
A 420-day warm-up does not reproduce Donchian state, EWMA or band-dependent weights. Python pads
pre-listing days with MISSING from the canonical origin, as the daily lake does. Actual gaps stay
MISSING; the running day is excluded.

Initialization requires successfully fetching the canonical history for the **whole frozen
universe**. A missing symbol or failed initial page records only diagnostics: no weight, ledger or
resume state is committed. Recovery retries from the origin, even if some bars were stored during
the failed attempt. After a committed initialization, the feed appends completed days. A lagging
symbol holds back finalization for all tracks.

### Funding and portfolio evaluation

Funding history is paginated from the first unfinished day until its required closing midnight.
Coverage requires every settlement on the UTC grid verified by `/fapi/v1/fundingInfo`, including
adjusted intervals (unadjusted symbols use Binance's standard 8-hour interval) and day-end; a
successful response, an empty response or a short page alone establishes no coverage. Missing,
duplicate or off-grid settlements remain unavailable. A failed interval-metadata fetch also blocks
finalization. Historical interval transitions need a separately verified settlement schedule rather
than assuming missing settlements are zero. A run can finalize only the fully verified prefix of days.

Python calls the backtest's `signal_inputs`, `size`, `apply_band` and `VARIANTS` directly. Closes
through day d determine weights for day d+1. Daily return is the equal-weight mean over active
symbols of `w * (close/open - 1) - 11 bp * abs(w - w_prev) - w * funding`, rounded to integer ppm.
Tests compare the daily stream with the backtest and show that the former 420-day initialization
changes canonical weights. Funding failures can still record decisions once the whole universe
has initialized, but cannot finalize their outcomes.

### Reconstruction and prospective recording

The accounting start is 2026-10-01. Days reconstructed after their daily close are
**retrospective reconstruction**, including October 1–8 when first run on October 9. Using
causal closes to reconstruct them does not make them forward evidence.

Each immutable weight stores its source close, actual Python decision time (`decided_at_ms`),
and database recording time (`recorded_at_ms`). A day is **prospectively recorded** only if that
same decision was already recorded before its closing outcome. Later retries preserve the first
recording time and verify the canonical payload rather than silently replacing it. A new decision
for an already completed day is always retrospective.

The 00:05 UTC job can record a weight after that day's open. Prospective here means recorded
**before the daily close**, and the dashboard states this explicitly; these remain hypothetical
open-to-close returns, not evidence of execution at the open.

### Persistence and scheduling

`forward_trend_weights`, `forward_trend_ledger` and `forward_trend_state` are append-only and
account-scoped under RLS. `commit_forward_trend_run` atomically commits rows and their state under
an account transaction lock. It checks the expected prior state, verifies duplicate payloads,
checks ledger/sample counts, and rejects conflicting or obsolete runs. Only committed revisions
can be resumed; newer startup diagnostics never replace them. Resume binds the parameter hash,
strategy version, history origin, accounting start and ordered frozen universe.

This PR updates the fresh-database table definitions. With no existing tracking data, rebuild the
empty application database from its schema files and the operational database from its baseline;
there is no conversion of existing tracking rows or additional upgrade migration.

Run once for **all registered accounts**, using the configured application/operational databases
and authenticated Python service:

```sh
npm run forward:trend -- --once
```

Run continuously (immediately, then daily at 00:05 UTC):

```sh
npm run forward:trend
```

`FORWARD_USER_ID` is optional; setting it restricts the job to that account. Account enumeration
is paginated, account failures do not prevent the remaining accounts from running, and each loop
includes newly registered users. The forward page's “Run trend now” always uses its authenticated
user automatically. Production hosting/scheduling remains in `docs/production-todo.md`.

### Dashboard statistics

The dashboard defaults to prospectively recorded days, with a separate reconstruction selector.
It shows both counts, and separate means, turnover, exposure and equity curves for each sample.
Ledger and decision reads paginate in stable day/track order against a captured committed snapshot,
so the API row cap cannot freeze curves or statistics.

Python owns all moments and the power calculation, using `benchmark.power`:
`mu_min = min_detectable_edge_per_day(n, sd)` and `required_days(mean, sd)`. The dashboard only
formats the saved values. **Minimum detectable edge** uses a normal approximation, independent
daily returns, two-sided alpha 0.05 and 80% power. It is a sample-size/power estimate, not a
significance test or a verdict on the strategy. See the [NIST sample-size discussion](https://itl.nist.gov/div898/handbook/prc/section2/prc222.htm).

### Committed candles and a pure evaluator

- **Committed candles:** `record_forward_daily_bars` compares each incoming row with a stored
  (symbol, day).
  - A differing payload raises `forward daily bar conflict: SYMBOL DAY`. It is never silently
    dropped, and the stored row is never overwritten.
  - The run records that day as a diagnostic (`partial`).
  - After writing, the feed reads the rows back, so Python always computes from the committed
    values.
- **No clock in Python:** the request requires `evaluated_at_ms`, which the orchestration supplies
  from its own clock. `trend_track.evaluate` never reads a wall clock, so identical requests give
  identical responses. The Postgres recording time (`created_at`) remains the authoritative record
  time.
