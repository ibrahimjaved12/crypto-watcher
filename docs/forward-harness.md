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
  - **Request size:** the local request still sends the whole 120-day TA window (up to
    120 x 1,440 x 6, about 1 million rows, roughly 100 MB of JSON across six sequential requests)
    to the local Python service. It is not sent to hosted Supabase. Each ok run records
    `freshness.request = {rows, python_ms}`. Local sigma state and daily hosted checkpoints
    reduce hosted traffic; they do not shrink this local indicator lookback.
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

## Strategy Lab reports and logs (P20)

- **No row caps.** Results, non-trades and the equity curve are aggregated in SQL over every row
  (`forward_outcome_report`, `forward_nontrade_report`, `paper_equity_series`, all `SECURITY INVOKER`, own
  rows through RLS). The old dashboard read capped lists (2,000 setups, 10,000 outcomes, 10,000 ledger
  lines ascending), which silently dropped the oldest trades and froze the equity curve. Logs are
  keyset-paginated (50 rows) and exported as CSV (up to 50,000 rows, formula-prefixed cells neutralised).
- **What a row says.** For each `(strategy, timeframe, reward:risk)`: trades, wins, unclear (ambiguous,
  counted pessimistically), mean net R with its standard error, the matched `placebo-v1:` control (same
  timeframe and exits), the Welch difference and z, and the non-trades (vetoed V, bad entry candle C, off-tick P,
  stop too tight G, not tradeable N) and entered trades still open.
- **Verdict rule (fixed).** n < 30: "too few trades". Else |z| < 2: "no difference from random timing";
  z >= 2: "better than random timing, unadjusted for K" with the Bonferroni critical value for K rows
  (K = strategy rows shown); z <= -2: "worse". It is a statement about this sample. A positive row is a
  hypothesis for the next sample, not an edge. The control mean and variance come from the same
  filters (dates, coin, direction), so both sides are compared on the same trades.
- **Filters** live in the URL (date range, strategy rule, timeframe, direction, coin, reward:risk).
  There is no score band: the six `ta-v1` baselines are binary rule crosses and no score exists in
  `forward_signals` / `forward_setups`. Score bands return when a graded strategy exists.
- Max drawdown is peak-to-trough on the bucketed equity series (hourly, daily beyond 60 days), so
  an intra-bucket dip is not seen.

## Parity run findings (BTCUSDT 2025-05, 1,736 setups)

The first real-month parity run failed rules (ii) and (v). Both are findings, with these causes:

- **Rule (ii), 0 of 1,736 setups with identical geometry.** The robust level is bit-identical (|delta| = 0) and
  every setup joins its label row (unmatched 0, all T/T). Geometry differs in all setups because of c_h alone: the
  forward request expands the c_h median from the start of its 120-day window, the label engine from 2024-01. Median
  |delta c_h| / c_h = 1.3 %, p95 1.6 %, hence sigma differs by the same and d_ticks by a few ticks; rules (iii) and (iv)
  pass (99.8 % within 3 %). Nothing short of giving the forward harness the same c_h history closes this: seed c_h from
  the lake or persist the calibration state per symbol (the ratio multiset, or the heaps of `_RunningMedian`, updated
  each hour). The pass rule is not loosened; rule (ii) keeps failing until then.
- **Rule (v), 73 of 1,736 resolutions differ by 1 micro-R in net.** Status and exit are identical; `fund_ur` differs by 1
  micro-R exactly on the setups that cross a funding settlement. Funding is paid on the settlement mark and the forward
  bars used the trade price as the mark proxy. Fixed: the funding history now carries the exchange mark
  (`markPrice` of `fundingRate`) and `evaluate` sets the settlement minute's mark with it (`apply_funding_marks`); the
  parity request passes the lake's settlement mark so the check compares like with like. Live uses the API's markPrice, which
  may differ from the lake's mark-kline open by a little; the parity report shows what remains.

### P26b / P27: funding parity and separate market/wallet evaluation

For unseeded rolling-history requests, rule (ii) stays **FAIL** because exact c_h state is absent (#263), so the overall verdict is **FAIL** even when rule (v) passes; P28a adds the seeded mode described below.
Rule (v) now applies settlement funding marks to its copied request bars before building funding,
exactly as the forward outcome engine does.

`POST /v1/forward/evaluate` keeps its existing request/response contract and TypeScript caller.
It composes `market_evaluate` once per symbol with the pure `wallet_step`; adapted bar arrays
are released after each symbol, rather than retained for a second liquidation pass.

The two additional authenticated routes are synchronous in the same forward Python process
as `/v1/forward/evaluate`, with no movement-process dispatch:

- `/v1/forward/market_evaluate`: `schema_version: 1`, `stage_run_id` (UUID), `symbol`,
  `rows`, optional `funding` and `funding_available`, `strategy_ids`, `from_ms`, `to_ms`,
  optional `open_setups` (market facts only), and optional sigma checkpoint fields.
  It accepts no positions or wallet configuration. Returns signals, setups, resolutions,
  reasons, processed time, events, adverse paths, versions, and advanced sigma state when enabled.
- `/v1/forward/wallet_step`: `schema_version: 1`, optional `state`, `events`,
  `adverse_paths`, and optional `config`. Returns wallet state, ledger, effective wallet
  configuration, and assumptions. Exact fractional configuration fields are strings,
  e.g. `risk_fraction: "1/100"`.

Persisted events contain `stage_run_id`, `symbol`, `sequence`, `ms`, and `type`
(`open`, `close`, or `funding`), plus their event payload. Duplicate
`(stage_run_id, symbol, sequence)` keys are rejected. Transport merging uses
`(ms, symbol, sequence)`; wallet execution order is unchanged: timestamp, then
liquidation before close before funding before open, with the existing setup-ID/stable ties.
Only the `/evaluate` composition honors its internal `compatibility_order` to preserve
historical caller-symbol ordering. The wallet API does not accept that override.

Each open or resolved setup carries an adverse path: `{setup_id, symbol, side, points}`,
where `points` is a chronological list of `[ms, worst_price]` record-breaking positive
mark extremes from entry through the final exit minute, inclusive, or the available stage end.
Shared evaluation excludes rows after `to_ms`; `/evaluate` preserves its historical behavior
of treating `to_ms` as the signal window while resolving against all supplied rows.
Longs use mark lows; shorts use mark highs. Existing trade-price proxies and settlement-mark
adjustments remain unchanged. The wallet derives a liquidation at the first long point
`<= floor(liquidation_price)` or short point `>= ceil(liquidation_price)`. New positions get
that price from `position_terms` with their user's leverage cap, buffer, and maintenance margin;
held positions retain their stored terms. Eligibility still comes from the original
portfolio-wide opens-only preflight, including caps and margin rejections. A close or adverse
path for a setup that the user never opened produces no ledger line and no error.
Neither wallet function mutates its inputs, reads a clock, or performs I/O.

Peak-memory expectation: separate one-symbol requests hold **one sixth of the rows** of a
six-symbol request with equal history lengths. This is a rows-held expectation, not a measured
sixfold reduction in process RSS: Python overhead, sigma working arrays, retained output events,
and wallet state still contribute. The compatibility endpoint still receives all six symbols'
raw rows; use sequential market requests followed by one wallet step to obtain the input-memory
benefit. Do not execute each symbol's wallet independently, because sizing and caps are portfolio-wide.

CI regressions use short synthetic inputs. P29 replaces the frozen pre-P27 evaluator and
its golden fixture/test with a three-configuration equivalence guard. It compares shared
replay and `/evaluate` with the former opens-only plus full mark-series liquidation algorithm,
covering liquidation, stop, target, rejected entry, funding, and simultaneous multi-symbol events.
The independent full-series scan remains in `liquidation_events` for that oracle.
These regressions were not run locally.

### P28a: exact sigma state

The c_h decision is an **exact expanding ratio list**, seeded once and appended chronologically
as windows complete. Neither a rolling-window length nor a longer request-window option is adopted.
`forward/sigma_state.py` stores EWMA `(A, W, n)` per half-life, published levels, the previous
block close, the current day's returns, the last 28 days of seasonal summaries, pending calibration
windows, each horizon's ratios and `first_counted_ms`, and `as_of_ms` (last consumed minute's open).
Advancing overlap is a no-op. Completed window opens at t enter the median before sigma(t);
the minute's close cannot affect sigma(t). A two-day decision cache supports normal hourly retries;
older decisions require rebuilding from history. Historical decisions produced during a long advance
are available to that evaluation without keeping them all in the persisted state.

The benchmark computes ratios using binary `log`/division and rounds only the final c_h. To preserve
its exact result, each stored ratio is a lossless integer numerator/denominator pair **in HCAL_SCALE
units**, rather than prematurely rounding the ratio to integer ppm. Median and final rounding match
`horizon_calibration`/`_RunningMedian`. All payload integers travel as decimal strings to survive
JavaScript JSON round trips; the envelope has a version, symbol, safe millisecond timestamp and SHA-256
checksum of canonical JSON excluding the checksum itself. Wrong versions/checksums are rejected.

Export a seed from already-downloaded lake files (exclusive cutoff, hidden-month guard applies):

```sh
python scripts/research/seed_sigma_state.py --bars-dir /path/to/lake --symbol BTCUSDT \
  --first-month 2024-01 --last-month 2025-04 --cutoff-ms 1746057600000 --output /tmp/BTCUSDT-sigma.json
```

`forward_parity.py --seed-sigma-state` (workflow input `seed_sigma_state: true`) seeds all available pre-month history before the existing
request, so rule (ii) can pass with identical sigma/geometry; an unseeded rolling-history run still
has the previously documented c_h discrepancy. A pass is measured by the report, not assumed.
This does not persist TA indicators or reduce their warm-up requirement to a few rows.

### P28b: persistence and hourly wiring

Apply both migrations: application DB `20261011130000_forward_sigma_state.sql` and operational DB
`20261011130000_forward_sigma_history.sql` (earliest stored minute discovery). The global
`forward_sigma_state` key is `(symbol, sigma_version)`; it contains market data only and is accessible
only by the service role. Account wallet/run/output records retain their existing ownership.

The production hourly runner loads and verifies **local-first** state, sends sequential `/v1/forward/market_evaluate`
requests with `use_sigma_state: true` and `sigma_state`, then submits the merged events to
`/v1/forward/wallet_step`. It still reads the **existing 120-day TA lookback**, extended back to
`as_of_ms + 1 minute` if needed. Indicators are not checkpointed, and this does not claim requests
shrink to a few rows. The sigma engine ignores already-consumed history in that lookback.

CLAUDE.md §4 (free tiers) decided local-first state and a daily checkpoint over the
hosted-every-hour design (refs #279, #273). `.forward-state/<SYMBOL>.json` at the repository root
is gitignored and contains `{state, hosted}`: the validated `SigmaStateRecord`, plus the last
hosted checksum, `as_of_ms`, and `checkpointed_at` in milliseconds (or `hosted: null`). Each
hour uses the validated local file without downloading hosted sigma. A missing or invalid file
restores only that symbol/version's hosted row, verifies its checksum, and writes the local file.
Only a missing hosted row permits a rebuild; invalid hosted data fails verification. Writes use
a temporary file and atomic rename under a per-symbol lock and never decrease local `as_of_ms`.
The advanced state is saved only after this process wins the market-stage commit. An existing
stage or a concurrent losing calculation does not save advanced state.

Apply application migration `20261011172231_forward_sigma_checkpoint.sql` after P29. After a
successful new stage, each symbol whose hosted checkpoint is missing or at least 24 hours old
gets one service-only `commit_forward_sigma_checkpoint` RPC. The RPC locks `forward-sigma:<symbol>`,
requires the expected checksum (including null for a missing row), rejects decreasing time, and
makes equal checksums a no-op. Success updates only that file's hosted metadata. Failures are
logged and retried next hour without failing the stage or wallets. A checksum conflict marks
`hosted.checkpointed_at: null` durably and restores from hosted on the next run; it never overwrites
the conflicting hosted row. If that restore is older than local state, evaluation uses the hosted
state while the disk cursor stays unchanged until a successful new stage saves advanced state.

Missing/version/checksum/identity-mismatched or out-of-cache state produces a `sigma_rebuild` log
and run reason. The runner discovers the first stored 1m candle and rebuilds in bounded 30-day
requests (`sigma_only: true`) before evaluating the decision interval. No other sigma model is used.
A rebuild from limited stored history cannot recreate an older lake origin: import the matching lake
seed to preserve that origin. If history remains insufficient, outcomes retain the existing no-sigma
behavior. Repairing candles at/before a valid checkpoint does not retroactively change it; corrections
require a deliberate reseed/version migration, not a silent alteration of past decisions.

P29 supersedes the combined `commit_forward_sigma_run` production path with the separate
market and user commits below. The old RPC remains available for rollback; the shared
production runner never calls it. Checkpoint locks and expected checksum checks now belong to
the separate daily RPC. Legacy functions and applied migrations remain unchanged for rollback.


Import an exported seed before enabling the hourly job:

```sh
# Credentials come from the existing service environment; do not put them in the file or command.
node scripts/seed-sigma-state.mjs /tmp/BTCUSDT-sigma.json
```

The loader reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`, checks the version and checksum, and
calls the service-only seed RPC. Re-importing the identical file is a no-op; a different existing
checkpoint is refused. This command does not run Python, start a server, or alter a wallet.

### P28c: provenance, gaps, and private seed export

Every state payload now includes `source: "lake_seed" | "store_rebuild"`, covered by its checksum.
`seed_state(..., source="lake_seed")` identifies a lake export; production initialization and
sigma-only rebuilds use `store_rebuild`. Advancing a state preserves its source. Untagged older
payloads are rejected and take the explicit rebuild path rather than being assigned an invented
origin. A store rebuild can have a different `first_counted_ms` and c_h from a full lake seed.
Filter forward evidence using `paper_runs.freshness.<symbol>.sigma_source`; each symbol also has
`sigma_payload_bytes`, the canonical UTF-8 size of the **payload**, excluding the envelope/checksum.
The runner logs this byte count without logging the payload. These measurements expose expanding
ratio-list growth and hourly JSONB rewrite cost; storage layout and median behavior are unchanged.

`advance` refuses a request starting after `as_of_ms + 1 minute` with `SigmaStateMismatch` before
mutating the state. The market endpoint also checks the first supplied collector minute, so 4h
adapter padding cannot disguise omitted history. The runner rebuilds or refuses such requests.
Interior missing candles still carry the explicit missing flags. A missing minute already consumed
outside the recent stale window stays missing in the checkpoint: a later REST fill at or before
`as_of_ms` is ignored by overlap/idempotency. If such a discrepancy appears, rebuild from the corrected
history deliberately; merely passing overlapping bars again does not repair the state.

The manual **Sigma seed export** workflow (`sigma-seed-export.yml`) runs only on `main`. Inputs:
`symbols` (comma-separated subset of the six), `cutoff` (exclusive UTC month boundary, e.g.
`2026-10-01T00:00:00Z`), and `plan_id` (required whenever the history intersects hidden months).
It loads verified rd-r1 releases from `2024-01` through the month before the cutoff, one symbol at a
time, using the same guarded loader as parity. Seeds and manifests are committed **only** to the
private research-data repository under `seeds/sigma/`; public script output contains counts and
checksums, never seed payloads or plan contents. The offline exporter remains useful for already
available development/validation lake files; sampled local days are not a production seed.

A production cutoff of October 2026 includes the hidden months January–September 2026. First register
and commit a plan in the private repo using the existing `Plan`/`write_plan` format; this workflow does
not generate or approve a plan. Alongside the normal private plan fields, its `variants` must be `1`
and `strategies` must contain exactly this technical entry (symbols sorted lexicographically):

```json
{"strategy_id":"forward-sigma-seed","strategy_version":"forward-sigma-v1","config":{"symbols":["BTCUSDT"],"cutoff":"2026-10-01T00:00:00Z","first_month":"2024-01","data_revision":1}}
```

Use the actual selected symbol list/cutoff in that private plan. Pass its full SHA-256 `plan_id` to
the workflow. The exporter verifies the committed plan matches the selection, then appends and
pushes `hidden-opens.jsonl` **before** downloading or reading hidden history. A failed log push aborts;
a repeat opening of the same question is refused, with no automatic force/reopen option. Development
seed exports do not require a plan.

Deployment order: **apply both migrations → import the exported seed → start the first hourly run**.
Retrieve the desired JSON from the private `seeds/sigma/<symbol>/` directory and import it with:

```sh
node scripts/seed-sigma-state.mjs /path/to/private-seed.json
```

Use the existing service environment for credentials. Identical imports are idempotent and a different
live checkpoint cannot be overwritten. Starting the hourly job without a seed explicitly uses
`store_rebuild` provenance; it is not equivalent to a lake-seeded run.

### P29: one shared hourly market stage, independent user wallets

Apply application migration `20261011150000_forward_shared_stages.sql` after P28.
`runMarketStage` checks only the persisted stage ID before reading sigma, evaluates each symbol
sequentially once, and commits the shared stage. `runUserStage` only reads committed stages
and replays them after the account cursor, in boundary order. A second user in the same hour
adds no market evaluation or sigma rebuild. The hourly job runs the market stage before
its user loop; account initialization and wallet failures are isolated per user.
A concurrent market calculation may race, but only one stage commits per boundary. The winner
uses its in-memory stage and event keys for every account; only the loser reads back the winning
stage. The latest-stage read selects only `id`, `boundary_ms`, and `payload->processed_to_ms`.
Replay lists stage IDs/boundaries and hydrates only cache misses. A per-process cache retains the
latest 48 hydrated stages across dependency recreation and account replays. After a restart,
older account cursors may need one hydration per missing stage.

If no market stage exists yet, the first decision range starts at the earliest registered
account's `start_boundary_ms`, including hours that failed before the first commit. Subsequent
ranges start at the previous market cursor. **Known risk:** a multi-hour outage is caught up in
one stage. `runMarketStage` merges all symbols' signals, setups, resolutions, events, and adverse
paths for that range, and `createSharedRepository.commitMarket` sends them together. That path
can produce the largest commit; this change measures it and does not split it or claim to fix
the statement timeout.

Shared tables have no `user_id`: `forward_market_stages`, `forward_market_events`,
`forward_market_paths`, `forward_market_signals`, `forward_market_setups`, and
`forward_market_outcomes`. They contain market facts only, are append-only, selectable by
authenticated users, and writable only by the service role. Dashboard market logs and report
RPCs read these shared facts. Sigma remains in the existing service-only checkpoint table.

`forward_wallet_accounts` stores the user's account ID, configuration, and start boundary.
`paper_runs` and `paper_ledger` retain their existing owner RLS (`auth.uid() = user_id`),
with no client writes. New accounts use their user UUID as account ID, start at the current
boundary, and consume only later stages; they do not open historical setups. The migration
maps existing users to their existing account ID/configuration and wallet snapshots, so the
owner's `default` ledger and sequence continue without copying or renumbering ledger lines.
`INSERT ... SELECT DISTINCT ON` backfills shared market facts and stage boundaries from legacy
rows. Historical stages have no reconstructed event stream. Old tables, columns, and RPCs
remain in place; this migration does not drop legacy data. Rollback requires restoring the old
application/report definitions; new shared hours are not mirrored into the legacy market tables.

- `commit_market_stage` serializes market commits and atomically inserts shared output. The hourly
  caller sends `p_states: []` and `p_expected: {}`; sigma is saved locally after success, and the
  separate daily checkpoint RPC handles hosted state. The existing function keeps its sigma
  handling for rollback. Repeating a boundary is a no-op and does not touch local state.
- `commit_user_run` locks the user, checks the expected previous successful run and next stage,
  and inserts that user's run/ledger atomically. Successful `(user, boundary)` retries are no-ops.
  `consumed_boundary_ms` is the cursor; retryable `skipped_stale`/`no_sigma` rows never advance it.
  Those market-stage skips also never persist advanced sigma or consume the market hour key.
  A user failure leaves the shared stage and other users' commits intact for a later retry.

`FORWARD_USER_ID` optionally selects a user, not a shared market namespace. Wallet configuration
changes are service-side only; there is no client configuration editor in this change.

Each computing run logs one `[forward-sigma] <SYM> source=local|restored|rebuilt as_of=<ISO>
checksum=<first 8 hex> bytes=<n>` per symbol. Checkpoints log
`[forward-sigma] checkpoint <SYM> written|failed (<message>) ms=<n>`. Market RPC attempts log
`[forward-commit] boundary=<ISO> stage_bytes=<n> records_bytes=<n> events=<n> adverse_paths=<n> rpc_ms=<n>`.
One `[forward-hosted] reads=<n> read_bytes=<n> writes=<n> write_bytes=<n>` summarizes the run, including
account listing, wallet-state reads, replay hydration, sigma restores, and checkpoint attempts.
Byte counts are UTF-8 JSON body sizes, excluding HTTP overhead/compression. `stage_bytes` and
`records_bytes` measure the market RPC arguments; `read_bytes`/`write_bytes` are cumulative snapshots
at each account commit in `paper_runs.freshness` (including that write). The final log also includes
subsequent account reads. A normal local-cache hour should read far below 1 MB; restores and
uncached catch-up stages are exceptions. Daily checkpoints write the roughly 2.4 MB state once per
symbol instead of sending/downloading all six states every hour. The owner's next logs and hosted
usage page determine the remaining stage size, timeout behavior, and actual egress.

The Python equivalence, TypeScript call-counter/failure-isolation tests, and PGlite transaction,
migration, idempotency, and RLS tests are committed for GitHub checks. No local tests, typecheck,
build, servers, or database migrations were run for P29; no runtime or CI-duration result is claimed.
The added Python fixtures contain only a handful of minutes, with no long sigma warm-up.
