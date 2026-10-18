Local setup and hosted profiles: [Environment setup](environments.md).

Frequent completed-candle working state can use a separate bounded
[operational PostgreSQL](operational-database.md). The main database remains the permanent application
database and the movement baseline/cooldown/alert transaction remains entirely in the main database.

# Crypto Watcher

**Status.** The crypto futures tool is the first market. A forex market (CFD-style spot and futures,
same repo and name, a dashboard Crypto/Forex toggle, the backend computes both) is being added,
tracked in [#374](https://github.com/ibrahimjaved12/crypto-watcher/issues/374); design in
[Forex](forex.md). The owner's intent lives in [`CLAUDE.md`](../CLAUDE.md) and is the source of
truth; issues and docs are hypotheses, not requirements.

Crypto Watch identifies and evaluates **conditional futures-trade opportunities
and profitability after costs**. The initial target is **Binance USDⓈ-M,
USDT-margined perpetual futures, long and short**. The intended lifecycle separates
current assessment, developing setups, activation, trade management, evaluation,
historical backtesting, and virtual-wallet paper trading. Real-money execution stays
manual for now (not permanently excluded); the automated simulation uses fake money only.

Read the [product direction and architecture](product-direction.md) for the
current and proposed data flows, futures evidence rules, news-context limits, and
service/database ownership. The
[analysis records, conditional setups, and evaluation specification](analysis-evaluation.md)
defines current and intended histories, evidence-bound setups, general trade scores,
score-band reports, and strategy outcomes. The
[futures paper-trading simulator](futures-simulation.md) defines the automated
virtual-wallet trader, execution costs, accounting, and historical/live paper runs.
The [implementation and deployment roadmap](roadmap.md) links the ordered
GitHub issues and their dependencies, distinguishing current, proposed, and
conditional decisions.

<!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days -->
The alert path tries public futures REST data in order: **Binance USDⓈ-M,
OKX USDT perpetual swaps, then Kraken perpetual futures**. The dashboard's price cards do not:
the Worker makes no exchange request except the temporary degraded-mode fallback, and reads the Binance USDⓈ-M quotes the owner's machine
publishes to hosted Supabase ([#323](https://github.com/ibrahimjaved12/crypto-watcher/issues/323),
[publisher](binance-futures-collector.md#market-quote-publisher-323)).
When all readable published quotes are stale or absent, the temporary fallback tries Kraken
futures, then OKX, with a banner and explicit venue/contract labels; see
[the runtime switch and removal condition](environments.md#temporary-degraded-market-fallback-323).
The [forward harness](forward-harness.md) exists: shared market stages, per-user wallet replay
and a paper ledger, run on the owner's machine by `worker:host`. Strategy profitability is **not**
validated; the `/forward` page carries a banner saying so.
Background execution also needs verification: [issue #13](https://github.com/ibrahimjaved12/crypto-watcher/issues/13)
reports an observed disabled cron; repository code alone does not prove a live schedule.

Technical Analysis v2 adds OHLCV indicators, numeric candle patterns, and saved
forward outcomes. See [TA rules and deployment](technical-analysis.md).

The dashboard and scheduled TA use a separate Python calculation service. See
[Python API setup and hosted testing](python-api.md)
for the server secrets, local/Docker commands and deployment steps.

The monitoring algorithm now has a [saved-baseline cumulative rule](cumulative-monitoring.md)
for gradual rises and falls. Its database migration must be applied before deploying
the updated monitor.

Market collection and completed-candle work now use request-scoped sharing,
due-work gating, bounded catch-up, batched outcomes, and locally measurable run
metrics. See the [monitor efficiency report](monitor-efficiency.md). This local
evidence does not claim a hosting billing reduction while the hosted cron is paused.

The futures cutover migration `20260923090000_binance_usdm_futures.sql` is destructive:
it clears watchlists, notes, alerts, baselines, checkpoints, TA snapshots, and monitor
runs, while preserving accounts and monitoring settings. Fresh watchlists are seeded
with Binance USDⓈ-M perpetual instruments on first use.

Temporary [activity controls](activity-controls.md) can make dashboard market
prices and TA history manual-only, skip authenticated scheduled monitor runs, and
independently pause TA generation or outcome evaluation. Automatic market refresh,
automatic TA-history refresh, and scheduled monitoring default to disabled; set their
flags to `true` to enable them. Manual Refresh and Run check now remain available.
These are operational switches, not evidence of lower billing.

User-scoped [independent activity controls](activity-domains.md) separately gate
market-data access, movement-alert generation, and completed-candle TA while preserving
pause/resume semantics. Future setup, paper-trading, email, and WhatsApp domains remain
fail-closed and are clearly labeled as unavailable.

## Production activity variables

| Setting                                         | Trigger and effect                                                                                                                        |
| ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Supabase scheduled job (not an environment variable) | Sends the scheduled monitor request every 5 minutes when the Job is enabled. A disabled Job sends nothing.                                |
| `SCHEDULED_MONITOR_ENABLED=true`                | Allows an authenticated scheduled request to run. It does not create a timer; with the scheduled job disabled, it causes no periodic calls. |
| `VITE_MARKET_AUTO_REFRESH_ENABLED=true` <!-- TEMPORARY degraded-mode fallback (#323): remove after the machine has run 24/7 for 30 days --> | Loads market prices when the dashboard opens and every 60 seconds while active. Uses the TanStack server and hosted quotes, except the temporary degraded-mode fallback.        |
| `VITE_TA_HISTORY_AUTO_REFRESH_ENABLED=true`     | Reads saved TA history when the dashboard opens and every 60 seconds while active.                                                        |
| `TA_GENERATION_ENABLED=false`                   | Skips new TA calculations during manual and scheduled monitoring runs. Unset defaults to enabled.                                         |
| `TA_OUTCOME_EVALUATION_ENABLED=false`           | Skips pending TA-outcome evaluation during manual and scheduled monitoring runs. Unset defaults to enabled.                               |
| `PYTHON_ANALYSIS_ENABLED=true`                  | Enables manual Python analysis and scheduled TA calculations. It does not create a second scheduler.                                      |

The user monitoring master switch gates manual and scheduled monitoring work. It does
not stop the scheduled job from calling the endpoint or stop either browser 60-second
refresh loop. `MONITOR_CRON_TOKEN` and the cron secret authenticate scheduled
requests; they do not schedule them.

Set `VITE_*` values in the committed `.env.production` and republish. Configure
non-`VITE_*` values in the production server environment. See
[environment setup](environments.md) for the complete variable list.

## Documentation index

### Product & plan
- [Product direction and architecture](product-direction.md) — owner intent summary, data flows, service and database ownership.
- [Analysis records, setups and evaluation](analysis-evaluation.md) — how analysis is recorded, setups scored and outcomes evaluated.
- [Implementation and deployment roadmap](roadmap.md) — ordered GitHub issues and their dependencies (provisional).
- [Plan reset v2: ticket proposals](plan-reset-v2-proposals.md) — AI-written ticket proposals awaiting owner decisions (#340).
- [Production to-do](production-todo.md) — remaining items for the forward-harness MVP.
- [Strategy catalogue v1](strategy-catalogue.md) — researched strategy list with roles, data needs and evidence grades.
- [Strategy specifications](strategy-specs/SPEC-0-master-plan.md) — SPEC-0 master plan, [SPEC-1 methods](strategy-specs/SPEC-1-methods.md), [SPEC-2 trend/TA/volatility](strategy-specs/SPEC-2-trend-ta-vol.md), [SPEC-3 levels/flow/positioning](strategy-specs/SPEC-3-levels-flow-positioning.md), [SPEC-4 exits/sizing/leverage](strategy-specs/SPEC-4-exits-sizing-leverage.md), [SPEC-5 data](strategy-specs/SPEC-5-data.md).

### Markets (crypto, forex)
- [Forex](forex.md) — second market (#374): instrument model, compatibility, data policy, PR roadmap.
- [Binance futures collector](binance-futures-collector.md) — the shared USDⓈ-M WebSocket collector worker.
- [Market movement engine](market-movement-engine.md) — collector-driven market-wide movement engine.
- [Cumulative monitoring](cumulative-monitoring.md) — saved-baseline cumulative movement alert rule.
- [Longer moves](slow-movement.md) — multi-hour movement descriptor (1 h / 4 h / 12 h); no alerts.
- [Technical analysis](technical-analysis.md) — TA v2 indicators, patterns and forward outcomes.
- [Candle patterns](candle-patterns.md) — pure candle feature/pattern reading layer.
- [Alert setup charts](alert-charts.md) — setup charts for movement alerts and pattern events.

### Data collection & lakes
- [Kline lake (rk-)](kline-lake.md) — 1-minute bars from monthly klines, 2020-12..2023-12.
- [Daily bars and funding history (dk1)](daily-history.md) — daily bars and funding for indicator warm-up.
- [Binance metrics archive](metrics-archive.md) — daily metrics files (open interest, long/short ratios).
- [Liquidation data probe](liquidation-data.md) — what the public liquidation archive holds; nothing published.
- [Operational PostgreSQL](operational-database.md) — local operational database for frequent working state.

### Forward harness & paper trading
- [Forward harness](forward-harness.md) — signals, setups, outcomes and paper wallet (forward test, no validated edge).
- [Futures paper-trading simulation](futures-simulation.md) — virtual-wallet trader, costs and accounting.
- [Forward worker outage runbook](forward-outage-runbook.md) — recovering scheduled forward jobs on the machine.

### Research & studies
- [Screening mode](screening.md) — cheap first look at a signal before a benchmark question.
- [Level-reaction event study L0](level-study.md) — do support/resistance levels bounce more than arbitrary prices.
- [Order-flow threshold ladder](order-flow-ladder.md) — of-v2a taker-imbalance threshold question.
- [Positioning family](positioning-family.md) — fading the account long/short ratio.
- [Seasonal sigma and labels-v2](seasonal-sigma.md) — seasonal EWMA sigma for trade labels.
- [Regime labels](regime-labels.md) — objective regime labels and calendar hypotheses.
- [Multi-day trend, Mode B](trend-mode-b.md) — portfolio evaluation of multi-day trend rules.
- [Pattern event study](pattern-study.md) — candle patterns as events over a complete candle table.
- [Market-state experiments](market-state-experiments.md) — registry of #123 market-state methods (superseded for predictors).
- [Historical replay](historical-replay.md) — chronological replay kernel (#35).
- [Historical study campaigns](github-historical-study.md) — start/stop/resume of the #163 study campaign.

### Operations, hosting & CI
- [Environment setup](environments.md) — local setup and hosted profiles.
- [Local runbook](local-runbook.md) — the forward-test MVP on one machine.
- [Worker host](worker-host.md) — `npm run worker:host` in production mode.
- [Cloudflare deploy](cloudflare-deploy.md) — deploying the web app to Cloudflare Workers.
- [Supabase setup](supabase-setup.md) — the hosted Supabase project.
- [Python API](python-api.md) — the Python analysis service and app integration.
- [Activity controls](activity-controls.md) — temporary switches for automatic entry points.
- [Independent activity controls](activity-domains.md) — user-scoped per-domain gates.
- [Monitor efficiency](monitor-efficiency.md) — local measurements of the monitor's processing.
- [PR verification](ci-verification.md) — the `Tests` workflow.
- [CI budget](ci-budget.md) — 2-minute budget, test tiers and selection.

## Original milestone brief

The following brief is retained as project history. Its original market scope,
milestone order, and trade-execution wording are superseded by the linked futures
product direction and roadmap, which include simulated execution only. This brief
does not claim that future milestones or the deployed schedule are working.

Build a personal crypto monitoring web app called Crypto Watch.

Overall goal:

Monitor market movements, show technical analysis and relevant news,

send alerts, and preserve analysis and hypothetical trade outcomes.

All actual trading stays manual. Do not implement trade execution.

Build this first milestone:

A working market dashboard with persistent storage and scheduled

price monitoring.

Platform:

- Use the hosted Supabase project for the database, authentication and backend.

- Require login and restrict each user's records to that user.

- Store records in actual database tables, not browser localStorage.

- Keep API secrets on the backend.

- Use a clean, responsive dark dashboard.

Watchlist and dashboard:

- Start with BTCUSDT, ETHUSDT and DOGEUSDT.

- Allow adding/removing supported trading pairs, up to 10.

- Fetch real public exchange prices and candles.

- Show current price and changes over 5m, 15m, 1h, 4h and 24h.

- Show a price chart, last successful update and data-source status.

- Clearly show unavailable or stale data. Never invent market data.

Monitoring:

- Implement server-side scheduled checks, initially every 5 minutes,

  so monitoring continues when my browser is closed.

- Confirm the platform supports this schedule and explain any limits.

- Make the price-change threshold and time window configurable.

- Save an alert when a threshold is crossed.

- Add a 15-minute cooldown per symbol and rule to prevent duplicates.

- Record failed checks and the last successful monitoring run.

- If the data provider is inaccessible from the hosting region,

  report that clearly instead of substituting simulated prices.

Persistence:

- Save watchlists, settings, alerts and personal notes.

- Each alert should include symbol, timestamp, observed change,

  comparison window, triggering rule and data source.

- Include searchable alert history and CSV export.

- Include a test-alert action, clearly marked as test data.

Future milestones:

Technical indicators and candidate setups; news collection and AI

summaries; email alerts; hypothetical entry/TP/SL outcomes and

performance reporting; WhatsApp later.

Keep the code modular for these additions, but implement only the

first milestone now. Explain what works, what needs configuration,

and any ongoing usage costs. Do not present placeholders as completed

integrations.

## Development

Pull requests run the [Python and Node verification checks](ci-verification.md).
PR checks run a tiered test suite with a 2-minute budget ([CI budget](ci-budget.md)).

Prefer working locally? Install Node.js with
[nvm](https://github.com/nvm-sh/nvm#installing-and-updating) and install Docker as
described in the [environment setup](environments.md).

```sh
git clone <this-repository-url>
cd <repository-name>
nvm install
nvm use
npm ci
npx supabase start
npx supabase db reset --local
npm run env:local
npm run dev
npm run dev:local
```

To reset both local databases:

```sh
npx supabase start
npx supabase db reset --local

npx supabase start --workdir operational-db
npx supabase db reset --local --workdir operational-db
```

For local migrations:
```sh
supabase migration list --local
supabase migration up --local
```
## Running the machine host

`npm run worker:host` supervises the crypto jobs today and will also supervise the forex gateway and
forex jobs (isolated crash domains) once those land ([worker-host](worker-host.md)).

First start Docker Engine:

```sh
sudo systemctl start docker
```
Then:
```sh
npm run up -- --update-worker-secret
```
1. Checks that Docker, cloudflared and your .env.production.local flags are fine.
2. Starts the local operational Supabase if it isn’t running.
3. Starts worker:host: the Python services, the collector, the forward jobs, the gateway and the jobs runner.
4. Starts a Cloudflare quick tunnel to the gateway and waits until its /health answers.
5. Writes the new tunnel URL into the Worker’s RUNNER_GATEWAY_URL secret, so the live site can reach your laptop.
6. Restarts the tunnel if it dies (the URL changes, and it updates the secret again). Ctrl+C shuts everything down cleanly..

To grant Crypto or Forex access:
```sh
npm run access:grant -- email@abc.com --label [label]
```
Label can be: crypto, forex, all

For the charts:
```sh
npm run alert-charts -- --mode production --event-id <alerts.id>
```
The repository pins Node 22 in `.nvmrc` and the Supabase CLI in `package-lock.json`.

Architecture: [operational PostgreSQL](operational-database.md) and the
[shared Binance futures collector](binance-futures-collector.md).

## Python analysis milestone

The separate [Python module](../python/README.md) provides a one-shot public-candle
analysis command and offline fixture tests. It does not change the running monitor,
authentication, database or scheduler. Read the [implementation inspection](../python/INSPECTION.md)
and [calculation specification](../python/SPEC.md) before integrating its results.
