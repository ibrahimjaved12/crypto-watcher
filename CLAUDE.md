# CLAUDE.md — Crypto Watch (crypto-watcher)

> v1, 2026-10-07. Written from the owner's original idea and the 2026-10-07 review session,
> and committed with the owner's approval. Review it when the research in #180 lands. The
> reconciliation of tickets/docs with this file is tracked in #181.

## 1. What is true and what is not

The **owner's intent in this file is the source of truth.** Everything else is a hypothesis:

- GitHub issues, PRs, `docs/*.md`, acceptance criteria, `python/SPEC.md`, roadmap order: all
  AI-generated. They are useful context, **not requirements**.
- Architecture, algorithm and threshold suggestions that came from the original ChatGPT
  conversations (e.g. "7 of 10 coins down 2% in 15 min", vendor picks, Django/Celery, the
  five-symbol pilot universe) are **provisional**, never final.
- If a ticket/doc/code conflicts with this file: **do not silently follow the ticket.** Say so,
  explain the conflict, and let the owner decide. Re-implementing or deleting already-built work
  is acceptable when it serves the intent.

## 2. What the product is

A personal **crypto futures detection + prediction + logging + auto-trading simulation tool**
(Binance USDⓈ-M perpetuals, long and short). The owner uses it for their own trading.
Real-money execution stays manual **for now** (do not treat that as permanently excluded or
required). The auto-trading _simulation_ (automated fake-money wallet, realistic futures costs)
is in scope and important.

Build it so it _could_ become a multi-user paid product later (account-scoped data, no
single-user shortcuts that would block that), but do not pay for services or over-build for
that until the tool proves it makes money.

### Two modes

1. **Current assessment / alerts**: "something is happening in the market now" (broad
   movement across liquid coins, TA conditions, later news context).
2. **Forward trade predictions**: "if X confirms, enter; exit by Y", e.g. a 4h candle closing
   at 74,200 implies the next 4h reaching 78,400; or price heading to resistance 80k or
   support 70k.

### What "prediction" means

- A prediction is a **conditional trade path**: entry condition, targets/resistance/support,
  stop, partial exits, invalidation, expiry.
- Predictions come in many forms (level hits, next-candle/trend direction, pattern
  completions). All are "strategies" and should be combinable in one analysis.
- **Success = a profitable trade after costs.** Price eventually touching a target is not
  success if the stop (or liquidation) came first. Any metric that ignores the stop is
  misleading. Beware benchmarks any naive strategy would pass (ranging coins revisit prices).
- A score such as 90/100 is a ranking, not a win probability.

### Capabilities wanted (all incremental, in parallel)

- Market-wide irregular-movement detection across ~10 liquid coins.
- Technical analysis as coded algorithms (not screenshots), TP/SL suggestions, leverage-aware.
- Logging: (a) signal/alert log, (b) outcome log judged by predefined rules, (c) paper-trading
  ledger. Filterable by date, strategy, score band.
- Historical replay/backtesting on real historical futures data; paper-trading wallet with
  realistic futures costs (fees, funding, slippage, liquidation).
- Later: ML/AI layers, only if they measurably beat simple baselines out of sample.

## 3. Current priorities (set 2026-10-07)

1. **Trading algorithms / maths / trade-prediction research**: find and test good strategies
   and predictors (level hits, next-candle/trend, volatility, order flow, etc.) from papers,
   books, articles. Research is done in chat, not by Claude Code.
2. **Reflect and reset the plan before implementing**: fix tickets/docs so they point at the
   intent, then implement.
3. Parked, do not work on unless asked: **news ingestion** (separate conversation, later),
   **email/WhatsApp/notification delivery**, other backlog polish.

Project is roughly 20% complete; things proceed incrementally and in parallel.

## 4. Principles

- **Functionality and business logic come first**, architecture and speed second. Never trade
  away the web app's required behavior for cleanliness or speed.
- Free hosting tiers until the product is proven useful (this does not apply to research data; see Data rule). Heavy historical computation runs on GitHub Actions (local
  is too slow).
- Python is the long-term home of trading/strategy logic; the frontend/orchestration stack may
  change. FastAPI is the current default; Django is _not_ adopted.
- Scientific hygiene (adopted because it is sound, not because a ticket says so): no look-ahead,
  point-in-time inputs, versioned immutable records, chronological splits, untouched test set,
  costs included, ambiguity marked not optimistically resolved, report sample sizes and
  non-trades.
- Splits (owner decision 2026-10-08): development 2024-01..2025-06, validation 2025-07..2025-12, hidden test 2026-01..2026-09, forward live from 2026-10. Tuning uses development and validation only. The hidden stretch is opened once per question, after a plan file is committed to the private research-data repo; the loader refuses hidden months without a plan id and every opening is logged. The old 12 #123 test days no longer have a special status.
- Benchmark universe and horizons (owner decision 2026-10-07): six symbols BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT, frozen for all predictor work (a 7th only via the point-in-time liquidity check in #183). Primary horizons 15m, 1h, 4h; 1m/5m only as data granularity, features and diagnostics; daily and longer skipped for now. Always test both directions (long and short).
- Data rule (owner decision 2026-10-07): research quality is never limited by data size or by what is already downloaded. If research needs data we do not have, acquire it (contiguous history, aggTrades, klines, funding, mark/index, and other sources). Any purchase of paid data still needs the owner's explicit OK for that purchase. The contiguous research dataset is being rebuilt in the private research-data repo (see Data lake plan); the owner's local files (sampled study days only) are just a convenience copy.
- Data lake plan (owner decision 2026-10-08, details in #183): do not rely only on the owner's local files. Rebuild a contiguous research dataset in the private `crypto-watcher-research-data` repo under new `rd-` release tags, produced by GitHub Actions straight from the Binance public archive (`data.binance.vision`), using the published **monthly** zips for finished months. Assets may go up to just under GitHub's 2 GiB limit (the old study tooling's 1 GiB part cap does not apply to the lake). Raw zips are mirrored with their checksums; derived 1m bars are separate releases. Core window 2024-01..2026-09 for six symbols; klines, mark/index/premium, funding and 5m metrics may extend back to 2020; aggTrades stay in the core window. Liquidations are deferred (no free contiguous source; a paid purchase needs the owner's OK). The builder is `.github/workflows/data-lake-build.yml` (dry run with `publish=false` first; the E4 probe proved the pipeline and has been removed). The existing 18 releases are not deleted until the first real symbol-month is published and verified. Measured: BTCUSDT monthly aggTrades 2024-01..2026-08 = 18.3 GB; all six symbols are roughly 76 GB raw (estimate, range 65-90 GB).
- Prefer abundant retained data (ML needs it); retention constants were placeholders.
- Experiment log (owner decision 2026-10-08): the research notebook of every evaluated variant lives in the PRIVATE research-data repo as experiments/<question>.jsonl, appended by the merge step of a run; the public repo holds only the harness code. It counts variants for the multiple-testing correction and is not the app's signal/outcome/paper-trading logs.
- Release naming: rd-SYMBOL-YYYY-MM-rN (raw + 1m bars), lb1-SYMBOL-FIRST_LAST-rN (trade labels), rk- reserved for the 2020-2023 extension; releases are published normally, never marked Latest, with readable titles.

## 5. Stack (current, provisional)

Lovable-hosted React/TanStack frontend + Supabase Postgres; Python (FastAPI + shared
calculation package under `python/market_analysis`); external operational Postgres for frequent
writes; Binance USDⓈ-M collector using WebSocket (live) + REST (bootstrap/gap recovery);
GitHub Actions for heavy studies. Providers: **Binance primary, OKX and Kraken as fallbacks** (owner-confirmed 2026-10-07).
Keep price types and contract identity explicit when a fallback is used.

## 6. Working rules for Claude Code

- Implement; do not research. Research and decisions happen in chat.
- Do not run tests, local servers or other token-heavy extras on your own (GitHub checks cover
  them). If something matters, ask at the end.
- Do not spawn agents. You may create a branch, push and open a PR (body 'Refs #N'; 'Closes #N' only when the ticket is fully done). Never rewrite pushed history.
- Be specific: correct architecture, correct math and trading-algorithm technicality.
- Include good improvements you notice rather than leaving them as optional notes.
- Lovable sync: never rewrite pushed git history; keep the connected branch working.
- Never invent prices, news, or certainty. The repo is public: no secrets, no private notes.
- Python CI budget: the whole Python job must finish in under 3 minutes wall-clock. New test modules should stay under ~10 s; heavier statistical acceptance tests use tests/slow.py and run in slow-tests.yml; update python/tests/shards.json if a module exceeds ~40 s.

## 7. Known drift under review (2026-10-07)

- No ticket owns choosing/evaluating actual predictive trading algorithms; the "EXP-75"
  methods (EWMA, CUSUM, Kalman, PELT, BOCPD, ATR, PCA, correlation, HMM, mark/trade, OI/funding,
  taker flow) are market-state descriptors. They may still be useful as features/filters.
- #123 (historical-market-state-study-v1) measures state quality and incremental information vs
  V1, not trade profitability. Recommended: pause (see discussion), its test days
  have no special status under the new splits.
- #31 contains an illustrative support-bounce example only; no real strategies are chosen.
- Roadmap order numbers differ between issue bodies and `docs/roadmap.md`.
- Roughly 30 closed tickets are replay/state-classifier/study machinery; setups, outcomes,
  paper trading, dashboard and backtests (#30-#33, #36, #38) are all still open.

## 8. Open decisions

- Research and tracking tickets: #180 (strategy research + benchmark), #181 (reset/reconcile).

- Final list of strategy/predictor families to build and benchmark (research pending).
- How to benchmark predictors against trade-level profit (stop-aware labels).
- Fate of #123/#152/#163 and the state-classifier work.
- Django vs FastAPI stays FastAPI unless reopened.
