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
