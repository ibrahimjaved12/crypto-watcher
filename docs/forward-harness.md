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

| Module | Does |
| --- | --- |
| `bars_adapter.py` | Collector 1-minute rows (doubles) become a benchmark `BarSeries` (integers at 10^8). Also builds 15/60/240 candles with `candles.build_candles`. |
| `signals.py` | The `FORWARD_STRATEGIES` registry and `generate_signals`. |
| `setups.py` | `build_setup`, using the labels-v2 geometry. |
| `outcomes.py` | `resolve_setup`: incremental resolution, identical to `labels.build_labels`. |
| `wallet.py` | `step(state, events, config)`: the paper wallet, pure and append-only. |
| `evaluate.py` | One evaluation; the body of the endpoint. |

## What is recorded

- **Signal** (`forward_signals`):
  - Fields: strategy id and version, symbol, decision time `signal_ms`, side, and horizon.
  - Id: `signal_id = sha256(strategy_id|version|symbol|signal_ms|side)`, so re-running is
    idempotent.
- **Setup** (`forward_setups`, immutable): one per `(k, rr)` of the grid. With `k = 2` and
  `rr in {1.5, 2}` that is two setups per signal, each with its own id; K is visible.
  - It saves every input: variance, the seasonal factor weight, sigma, the entry open `p0`,
    tick, `d_ticks`, stop and target prices, the label leverage, the time limit, the label
    parameter hash (`params_hash`) and every component version.
  - Its status is T for tradeable, or one of the non-trade codes of the labels: V, C, P, G, N.
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
- **Sigma:** the seasonal model (`sigma_model=ewma-seasonal`).
  - The intraday factors of day D use only days before D.
  - The deseasonalised EWMA uses closes up to the decision.
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
- **Staleness:** the orchestration skips a run (`skipped: stale`) when candle history has gaps or
  the collector is not LIVE. No signals are generated from incomplete data.

## Comparison with backtests

Forward resolution *is* the benchmark label. A test builds labels-v2 for a synthetic fixture
with `build_labels` and checks that every forward setup and resolution equals the label row
field by field: status, p0, sigma, d_ticks, leverage, outcome, net/cost/funding micro-R and
ambiguity.

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

The daily trend Mode B variants (`benchmark/trend.py`, K = 9) are a daily weight stream, not
discrete trades. They are tracked forward as **hypothetical vol-targeted portfolios**, one per
variant. There is no margin, liquidation or position-limit model. No variant has a validated edge:
development-ext gives t of about 2, and validation is underpowered.

### Feed

- **Table:** the operational `forward_daily_bars` (symbol, UTC day, OHLC, volume, quote volume,
  source, received_at).
- **Write rules:** append-only, unique per symbol and day, and completed days only. A row must be
  received after its day ended (a CHECK constraint); `parseBinanceDailyKlines` also drops the
  running day.
- **Source:** the public `GET /fapi/v1/klines?interval=1d` (limit 1500).
  - The first run backfills from a **fixed** start, 2025-08-07: 420 days before the track start,
    enough for the 360-day lookback and the rvfilter's 395 closes.
  - Every later run appends only the days completed since the last stored one.
  - The start is fixed so that the band-dependent weight paths are identical from run to run.
- **Gaps** stay MISSING. Days are finalised only through the last day **every** symbol has stored,
  so a lagging fetch never turns into a permanently flat day.
- **Funding:** the public `/fapi/v1/fundingRate`, fetched once per symbol per run from the first
  day not yet finalised.

### Python (`forward/trend_track.py`, `POST /v1/forward/trend`, stateless)

- **Reuse:** it calls `trend.signal_inputs`, `trend.size`, `trend.apply_band` and the nine
  `trend.VARIANTS` directly; nothing is copied.
- **`target_weights`:** the weight held on day d+1 uses closes through day d (the backtest's
  one-day shift).
- **`step_portfolio`:** one day, using the same float operations in the same order as `trend.net`
  and `trend.portfolio`:
  - `w * (close/open - 1) - 11 bp * |w - w_prev| - w * funding`
  - The equal-weight mean over the active symbols, in integer ppm.
  - The equity curve, turnover, gross exposure, n, sum and sum of squares, and
    `mu_min = power.min_detectable_edge_per_day(n, sd)`.
- **Parity:** the daily stream is tested byte-equal, in ppm and day by day, to
  `trend.evaluate_variant` (the per-variant body of `evaluate_trend`) and to its buy-and-hold
  controls on a fixture with gaps and funding.
- **Funding unknown:** if a fetch failed, or a day is not covered (a full 1000-row page covers
  only up to its last event), **nothing is finalised**. The run records `funding_unavailable`, is
  `partial`, and a later run continues. Weights are still decided and stored.

### Controls (from day one)

- **`bh_vt_<sizing>_<target>`:** the vol-targeted buy-and-hold of each (sizing, sigma target)
  pair. This is the benchmark of the backtest's alpha test.
- **`ew_long`:** a long-only equal-weight portfolio (w = 1 per symbol).
- The circular-shift placebo needs the whole sample and is not computable forward.

### Persistence and schedule

- **Tables:** `forward_trend_weights`, `forward_trend_ledger` and `forward_trend_state`.
  - They are append-only and account-scoped (RLS), and every row stores the version and
    parameter hash.
  - Weights and ledger rows are unique per (track, day). They are written before the run's state
    row, so a state never claims a missing day.
- **Runs:** once a day at 00:05 UTC (`npm run forward:trend`; `--once` for one run) and on demand
  from the forward page.
  - A finished day has run key `day:<last completed day>`. A repeat is `already_done`.
- **Track start:** the track starts on 2026-10-01 (forward live from 2026-10). Closes from the
  hidden months 2026-01..09 are used only as indicator warm-up, from the live API rather than the
  lake. No result of a hidden day is computed or shown.

### Dashboard

For each variant, the forward page shows:

- days tracked and the mean daily return, against its buy-and-hold control and `ew_long`;
- turnover, gross exposure and the current weights;
- equity curves (variant, control and `ew_long`);
- the line "n days, not significant: mean < mu_min (about N days needed)", with
  `mu_min = 2.8016 * sd / sqrt(n)` from the live sample.

A banner says this is a hypothetical track with no margin or liquidation model.
