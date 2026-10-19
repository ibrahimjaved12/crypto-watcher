# CLAUDE.md — Crypto Watch (crypto-watcher)

> v2, 2026-10-16. Written from the owner's original idea and the 2026-10-07 review session,
> and committed with the owner's approval. Review it when the research in #180 lands. The
> reconciliation of tickets/docs with this file is tracked in #340 (successor of #181).

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
- **Success as a profitability verdict = a profitable trade after costs.** Price eventually
  touching a target is not success if the stop (or liquidation) came first. A metric that
  ignores the stop is misleading as a profitability claim.
- Always report beside it, never instead of it: (a) direction accuracy at the signal's
  horizon vs the unconditional base rate, (b) gross (before-cost) barrier results across
  the stop/target grid, (c) MFE/MAE distributions. These answer "does the signal predict"
  and inform leverage, stop and target choices.
- A score such as 90/100 is a ranking, not a win probability.

### Capabilities wanted (all incremental, in parallel)

- Market-wide irregular-movement detection across ~10 liquid coins.
- Technical analysis as coded algorithms (not screenshots), TP/SL suggestions, leverage-aware.
- Logging: (a) signal/alert log, (b) outcome log judged by predefined rules, (c) paper-trading
  ledger. Filterable by date, strategy, score band.
- Historical replay/backtesting on real historical futures data; paper-trading wallet with
  realistic futures costs (fees, funding, slippage, liquidation).
- Later: ML/AI layers, only if they measurably beat simple baselines out of sample.

Forex is in scope as a second market next to crypto (#374): CFD-style spot and futures, same repo and name, dashboard Crypto/Forex toggle, backend computes both. Approved scope: section 6; design and decisions: docs/forex.md.

## 3. Current priorities (set 2026-10-07)

1. **Trading algorithms / maths / trade-prediction research**: find and test good strategies
   and predictors (level hits, next-candle/trend, volatility, order flow, etc.) from papers,
   books, articles. Research is done in chat, not by Claude Code.
2. **Reflect and reset the plan before implementing**: fix tickets/docs so they point at the
   intent, then implement.
3. Parked, do not work on unless asked: **news ingestion** (separate conversation, later),
   **email/WhatsApp/notification delivery**, other backlog polish. The market-wide breadth alert
   needs research first (#373). Multi-day trend families (#222) are in the backlog. Paper trading
   (#36, #38) stays in the backlog until a few strategies survive costs (owner decisions 2026-10-16).

4. **Forex (#374)**, in parallel and owner-led on strategies (section 6): the open items are a signed-in MT5
   terminal with the gateway running (the code exists, #452 and #453, but nothing is running), the `All`
   medium/high calendar fan-out rule, the Forex Factory terms of use, and keeping CI under budget as the forex
   suites grow (#406).

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
- Benchmark universe and horizons (owner decision 2026-10-07): six symbols BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT, frozen for all predictor work (a 7th only via the point-in-time liquidity check in #183). Primary horizons 15m, 1h, 4h; 1m/5m only as data granularity, features and diagnostics. Daily and weekly candles are in scope for candle-pattern and multi-day studies (owner decision 2026-10-15, #348); the label engine and strategy registry remain 15m/1h/4h. Always test both directions (long and short).
- Data rule (owner decision 2026-10-07): research quality is never limited by data size or by what is already downloaded. If research needs data we do not have, acquire it (contiguous history, aggTrades, klines, funding, mark/index, and other sources). Any purchase of paid data still needs the owner's explicit OK for that purchase. The contiguous research dataset is being rebuilt in the private research-data repo (see Data lake plan); the owner's local files (sampled study days only) are just a convenience copy.
- Data lake plan (owner decision 2026-10-08, details in #183): do not rely only on the owner's local files. Rebuild a contiguous research dataset in the private `crypto-watcher-research-data` repo under new `rd-` release tags (and `rk-` kline-bar tags for 2020-12..2023-12, see the 2026-10-15 decision below), produced by GitHub Actions straight from the Binance public archive (`data.binance.vision`), using the published **monthly** zips for finished months. Assets may go up to just under GitHub's 2 GiB limit (the old study tooling's 1 GiB part cap does not apply to the lake). Raw zips are mirrored with their checksums; derived 1m bars are separate releases. Core window 2024-01..2026-09 for six symbols; klines, mark/index/premium, funding and 5m metrics may extend back to 2020; aggTrades stay in the core window. Liquidations are deferred (no free contiguous source; a paid purchase needs the owner's OK). The builder is `.github/workflows/data-lake-build.yml` (dry run with `publish=false` first; the E4 probe proved the pipeline and has been removed). The existing 18 releases are not deleted until the first real symbol-month is published and verified. Measured: BTCUSDT monthly aggTrades 2024-01..2026-08 = 18.3 GB; all six symbols are roughly 76 GB raw (estimate, range 65-90 GB).
- Intraday development extension (owner decision 2026-10-15, in chat): intraday development extends back to 2021-01 on 1-minute bars built from the Binance monthly 1m klines (`rk-` releases, 2020-12 as warm-up; `.github/workflows/klines-build.yml`); aggTrades stay 2024-01 onward. In the candle table this is the intraday segment `development-ext` (2021-01..2023-12, `bar_source` kline), and an intraday study's development means development-ext + development. Validation 2025-07..2025-12 and hidden 2026-01..2026-09 are unchanged.
- Daily/weekly universe (owner decision 2026-10-15, in chat): the daily/weekly tier may add extra liquid symbols (`data_lake.DAILY_EXTRA_SYMBOLS`, chosen from the liquidity-check table with a rule fixed before results); the six above stay the frozen set for all intraday work.
- Daily table start (owner decision 2026-10-15, in chat): the candle table's daily/weekly rows start 2020-01; rows whose indicator warm-up is not met are insufficient, never filled.
- Prefer abundant retained data (ML needs it); retention constants were placeholders.
- Experiment log (owner decision 2026-10-08): the research notebook of every evaluated variant lives in the PRIVATE research-data repo as experiments/<question>.jsonl, appended by the merge step of a run; the public repo holds only the harness code. It counts variants for the multiple-testing correction and is not the app's signal/outcome/paper-trading logs.
- Release naming: rd-SYMBOL-YYYY-MM-rN (raw + 1m bars), rk-SYMBOL-YYYY-MM-rN (raw 1m kline/mark/index/premium zips + 1m bars built from klines, 2020-12..2023-12), lb1-SYMBOL-FIRST_LAST-rN (trade labels, labels-v1, EWMA sigma), lb2-SYMBOL-FIRST_LAST-rN (trade labels, labels-v2, seasonal EWMA sigma; lb1 is never touched), lb3-SYMBOL-FIRST_LAST-rN and lb3h-SYMBOL-FIRST_LAST-rN (trade labels, labels-v3, robust |r| sigma and its horizon-calibrated variant; lb1/lb2 untouched), mx-SYMBOL-YYYY-MM-rN (Binance metrics archive), dk1-SYMBOL-FIRST_LAST-rN (daily bars and funding history 2020-2026, indicator warm-up), releases are published normally, never marked Latest, with readable titles.

## 5. Stack (current, provisional)

Cloudflare Workers frontend + own Supabase (accounts, published results) + local operational DB;
external operational Postgres is no longer planned; Python (FastAPI + shared calculation package
under `python/market_analysis`); Binance USDⓈ-M collector using WebSocket (live) + REST (bootstrap/gap recovery);
GitHub Actions for heavy studies. Providers: **Binance primary, OKX and Kraken as fallbacks** (owner-confirmed 2026-10-07).
Keep price types and contract identity explicit when a fallback is used.
Binance is blocked from Cloudflare Worker egress; the machine publishes Binance quotes to hosted `market_quotes` and the Worker only reads them (#323). Kraken/OKX are a temporary Worker fallback, removed after 30 days of machine uptime (#339).
Setup charts are rendered on the owner's machine from windows captured at event time; movement-alert charts also have a private hosted copy shown on the Alerts page (#299, #298; docs/alert-charts.md).

## 6. Forex (owner-approved scope; details and status in docs/forex.md, tracked in #374)

Approved decisions and facts. Anything marked UNVERIFIED in the code or docs stays unverified here.

- **Two markets, Crypto and Forex.** The Crypto/Forex toggle in the header is UI-only. Entitlement to a
  market comes from the plan (`has_market()`, `my_markets()`). Each market has its own paper account
  (`forward_wallet_accounts` per market) and its own tables (`fx_market_*`).
- **Data is free:** Exness MT5 demo for CFDs and PMEX MT5 demo for futures. Real-money trading
  automation is parked and out of scope.
- **The owner leads forex strategy work.** Claude does not invent strategies or tune parameters on its own;
  it builds tooling, data hygiene and honest tests of the owner's ideas.
- **News blackout filter.** The Forex Factory weekly feed (via the calendar fetcher, medium and high impact
  only) suppresses signals near events; suppressed signals are logged with the reason, never dropped
  silently. The feed's terms of use have NOT been checked, so `FX_CALENDAR_ENABLED` stays off until the
  owner confirms.
- **Wallet (owner decisions, 2026-10-19):** one position per setup (every tradeable setup of every enabled
  strategy opens its own paper trade, including both `rr` setups of one signal); 1% risk per trade; 1,000 USD
  start; total open notional capped at 10x balance; the position-count cap defaults to 1000 (effectively none,
  still configurable); a trade that cannot open is recorded as rejected with its reason.
- **Where things run.** The laptop worker host runs the forex children (`fx-calendar`,
  `fx-quote-publisher`, `forex-collector`, `forex-hourly`), each off by default behind `FX_CALENDAR_ENABLED`,
  `FX_QUOTE_PUBLISHER_ENABLED`, `FX_COLLECTOR_ENABLED` and `FX_HOURLY_ENABLED`, set in the host's
  `.env.production.local` only (never a Cloudflare variable). The collector child (#453) exists but is off and
  still needs a running, signed-in MT5 terminal and the gateway (#452, in `mt5/gateway`); neither runs yet.
- **Two databases.** The local operational Supabase holds bars, calendar, specs and collector health; the
  hosted main Supabase holds `fx_market_*` and accounts.
- **Runner access.** The runner is reached through a fixed Tailscale Funnel address (`RUNNER_PUBLIC_URL` on
  the host; `RUNNER_GATEWAY_URL` and `RUNNER_GATEWAY_SECRET` as Cloudflare runtime secrets and
  `RUNNER_GATEWAY_ENABLED` as a runtime variable). The quick tunnel was removed (#431).

## 7. Working rules for Claude Code

- Implement; do not research. Research and decisions happen in chat.
- Do not run tests, local servers or other token-heavy extras on your own (GitHub checks cover
  them). If something matters, ask at the end.
- Do not spawn agents. You may create a branch, push and open a PR (body 'Refs #N'; 'Closes #N' only when the ticket is fully done). Never rewrite pushed history.
- Be specific: correct architecture, correct math and trading-algorithm technicality.
- Include good improvements you notice rather than leaving them as optional notes.
- Never invent prices, news, or certainty. The repo is public: no secrets, no private notes.
- Python CI budget: the whole Python job must finish in under 2 minutes wall-clock. New test modules should stay under ~10 s; heavier statistical acceptance tests use tests/slow.py and run in slow-tests.yml; update python/tests/shards.json if a module exceeds ~40 s.

## 8. Known drift under review (2026-10-07)

- #31 contains an illustrative support-bounce example only; no real strategies are chosen.
- Roughly 30 closed tickets are replay/state-classifier/study machinery; setups, outcomes,
  paper trading, dashboard and backtests (#30-#33, #36, #38) are all still open.
- Lovable-era tickets #49, #51 and #41 (closed 2026-10-13; their bodies still describe Lovable-era plans): pending owner decision.

## 9. Open decisions

- Research and tracking tickets: #180 (strategy research + benchmark).

- Final list of strategy/predictor families to build and benchmark (research pending, owner-led).
- How to benchmark predictors against trade-level profit (stop-aware labels).
- Always-on host: not needed during development (decided 2026-10-13); revisit at about 80% completion.
- Doji work: paused by the owner on 2026-10-16; see #361-#366 (resume order in the #364 comments).
- Django vs FastAPI stays FastAPI unless reopened.
- Forex (#374, section 6): running the MT5 terminal and gateway (the collector child and the gateway code exist,
  #452 and #453); the rule for `All`-currency medium/high calendar events (today such a row stops the parse; it
  needs a fan-out decision); the Forex Factory feed's terms of use (`FX_CALENDAR_ENABLED` stays off until
  confirmed); the CI budget (#406).
