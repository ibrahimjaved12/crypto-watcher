# Product direction and architecture

Status: current project decision, recorded 2026-09-18. Provider prices and free-tier
limits are a dated planning snapshot and must be checked again before a deployment
decision.

## Product purpose

Crypto Watch is a personal crypto market decision-support and evidence system. Its
first user is one person making manual trades. It should detect important market
movement, add reproducible technical and news context, deliver timely alerts, and
record what was known and what happened afterward.

The product should help answer four different questions without conflating them:

1. **Detection:** Is an unusual movement or market-wide event happening now?
2. **Context:** What do price structure, volatility, participation, and current news
   say about it?
3. **Hypothesis:** Does a defined, versioned method estimate a directional outcome
   or candidate setup?
4. **Evidence:** How did that hypothesis perform after realistic time horizons and
   costs?

Technical indicators are measurements of market history, not independent proof of
what happens next. News summaries are context, not verified causes. A model output
is not a confidence score until calibration and out-of-sample evaluation support
that interpretation.

## Goals

- Monitor a selected crypto market continuously without requiring an open browser.
- Detect both per-symbol movement and broad movement shared across several assets.
- Calculate transparent, versioned technical analysis from validated market data.
- Collect timely source-attributed news and event context, including relevant public
  statements, macro events, and market disruptions.
- Keep fast movement detection separate from completed-candle technical analysis so
  an intrabar event is not ignored or mislabeled as a completed signal.
- Preserve immutable analysis inputs, outputs, source, timestamps, and version so a
  result can be replayed and audited.
- Measure later outcomes before describing a method as predictive or useful.
- Support historical replay and backtesting through the same calculation code used
  for live analysis.
- Deliver alerts through the dashboard first, with email and WhatsApp as later
  delivery channels.
- Eventually present hypothetical entry, take-profit, stop-loss, and leverage-aware
  risk scenarios when they can be tested and explained.
- Keep actual order placement and final trading decisions with the user.

## Non-goals and boundaries

- No automated trade execution, exchange custody, or autonomous position management.
- No promise of profit, prediction accuracy, or protection from market losses.
- No invented prices, candles, news, explanations, or missing-data substitutions.
- No use of an LLM response as the original source of a market fact.
- No screenshot-based chart interpretation as the canonical analytical record. Raw,
  timestamped market data is reproducible and suitable for replay; screenshots are not.
- No combined score merely because several correlated indicators agree.
- No silent fallback to a different calculation version, exchange, or implementation.
- No production ML probability presented as confidence until it is calibrated and
  evaluated on later, unseen data.

AI may later help retrieve, extract, deduplicate, classify, and summarize news, or
explain structured evidence. Every factual claim needs a source and event/publication
time. Access to X/Twitter, private communities, or licensed feeds depends on their
actual APIs and terms; a language model does not provide reliable feed access by
itself. Agent-style automation is a future orchestration option, not authority to
trade or fabricate missing evidence.

## Current deployed system

The following describes the current application. It is separate from the intended
direction below.

| Capability                 | Current behavior and owner                                                                                                                                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Dashboard quotes           | While the dashboard is open, the browser calls a TanStack server function every 60 seconds. The server fetches public exchange candles and returns prices, rolling changes, and chart data. Quotes are not stored.                   |
| User data                  | The browser uses the Supabase publishable client plus the signed-in user's JWT. PostgreSQL RLS scopes watchlists, settings, notes, alerts, runs, baselines, and TA history to that account.                                          |
| Background monitor         | An externally configured Lovable job calls the authenticated TanStack monitor route. The reported deployment schedule is every five minutes; the schedule and target are not defined by repository code.                             |
| Movement alerts            | TanStack loads completed one-minute observations and calls a PostgreSQL transaction that owns cumulative baseline state, cooldown checks, alert insertion, and baseline advancement.                                                 |
| Saved technical analysis   | The TanStack monitor calculates TA v2 for completed 15m, 1h, and 4h candles and writes versioned snapshots to `ta_signals`. Repeated runs use a uniqueness constraint to avoid duplicate snapshots.                                  |
| Outcome evaluation         | The TA path measures the existing saved-signal outcome definition when its target candle becomes available. This is descriptive forward return, not a simulated trade result.                                                        |
| Manual check               | An authenticated TanStack server function runs the same movement and TA workflow for the caller.                                                                                                                                     |
| Manual Python analysis     | TanStack verifies the user, reads that user's watchlist/settings/baseline, and calls the Render FastAPI service with a server-only token. Python returns a read-only result and cannot write alerts, baselines, runs, or TA signals. |
| Storage and authentication | Lovable Cloud/Supabase PostgreSQL and Supabase Auth.                                                                                                                                                                                 |
| Hosting                    | React/TanStack runs on Lovable; the optional FastAPI analysis service runs on Render.                                                                                                                                                |

The current request paths are intentionally different:

```text
Open dashboard quote refresh
Browser -> TanStack market function -> public exchanges -> browser

Ordinary account data
Browser + user JWT -> Supabase API -> RLS-protected rows

Scheduled monitoring
Lovable job -> TanStack monitor -> exchanges
                                -> PostgreSQL RPC/tables

Manual Python preview
Browser + user JWT -> TanStack analysis bridge -> FastAPI -> exchanges
                       |                          |
                       -> user-scoped DB reads   -> read-only result
```

Not currently implemented: news ingestion, AI news summaries, email/WhatsApp alert
delivery, historical candle replay, strategy backtesting, calibrated ML predictions,
combined technical/news hypotheses, or entry/TP/SL simulation. None should be shown
as available until its data path and failure behavior work in production.

## Intended architecture

The intended next-state boundary is a deliberate hybrid. Parts of it already exist;
making Python the canonical TA implementation is still future work. This is not a
commitment to migrate every backend operation to one framework.

| Area                                                | Owner under the current decision | Reason for the boundary                                                   |
| --------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------- |
| Dashboard and browser behavior                      | React/TanStack                   | Presentation and browser request lifecycle                                |
| Authentication                                      | Supabase Auth                    | Issues the identity used by database RLS                                  |
| User-scoped CRUD                                    | Browser/TanStack with Supabase   | Authorization remains enforced at the data boundary                       |
| Privileged orchestration                            | TanStack server on Lovable       | Holds current server credentials and is invoked by the existing scheduler |
| Atomic baseline and alert state                     | PostgreSQL RPC                   | Locking, idempotency, and state changes remain one transaction            |
| Market calculations, replay, backtesting, future ML | Shared Python package            | Live and historical execution must use the same versioned logic           |
| Live Python transport                               | FastAPI                          | Validates requests and adapts the Python package to HTTP                  |
| Scheduled trigger                                   | Lovable job                      | Current external trigger; frequency is deployment configuration           |
| Durable storage                                     | Lovable/Supabase PostgreSQL      | Current application database and RLS authority                            |

```text
React dashboard -------------------------------> Supabase API + user JWT/RLS
       |
       +---- analysis/manual action ----> TanStack orchestration
                                               |
Lovable scheduled trigger ---------------------+
                                               |
                                               +----> FastAPI adapter
                                               |          |
                                               |          v
                                               |     shared Python package
                                               |          ^
                                               |          |
                                               |     replay/backtest runner
                                               |
                                               +----> PostgreSQL RPC/tables
```

TanStack and Python are two runtime components, but they do not need to contain two
independently evolving definitions of the same analysis. Python should become the
canonical home of reusable market-analysis logic. TanStack may continue to own
application access and persistence while that remains the safer and more economical
boundary. A later control-plane migration is optional.

## Python package boundary

Reusable analysis must be ordinary Python code, not code embedded in FastAPI routes.
Its public operations should have explicit, serializable inputs and outputs and must
not depend on:

- HTTP request or response objects;
- FastAPI dependency injection;
- a database connection or user identity;
- environment variables or deployment secrets;
- the process wall clock hidden inside a calculation;
- global mutable caches or scheduler state.

Time-sensitive operations receive an explicit `as_of` time. Inputs identify the
exchange/source, symbol, timeframe, candle timestamps, completion state, and strategy
version. Outputs contain deterministic values, reason codes, missing-data status,
and enough metadata to validate freshness and provenance.

Adapters surround that core:

```text
                       +-------------------------+
FastAPI request ------>|                         |------> versioned response
Scheduled collector -->| shared Python analysis |------> persistence adapter
Historical replay ---->|        package          |------> research dataset
Backtest runner ------>|                         |------> evaluation report
                       +-------------------------+
```

FastAPI authenticates and validates live requests, applies timeouts, and serializes
results. Replay and backtests import the package directly; they must not make an HTTP
round trip to a deployed API. Provider clients, database writers, queues, and alert
delivery remain adapters rather than hidden calculation dependencies.

When TA v2 moves from TypeScript, the first Python version must preserve formulas,
lookbacks, initialization, null rules, classifications, scores, and explanations.
Both implementations must be compared against identical fixed candle fixtures. New
indicators or strategy changes belong in a later, separately versioned change. After
the production cutover, retire the TypeScript calculator so only one definition
continues to evolve.

## Workload separation

Different workloads need different timing and evidence rules.

| Workload                 | Trigger and data                                                                             | Persistence rule                                                                                    |
| ------------------------ | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Dashboard refresh        | Every 60 seconds while the page is active; visual convenience                                | Do not persist routine quote responses                                                              |
| Movement detection       | Scheduled observations, eventually as frequent as justified by latency and cost measurements | Persist state changes, qualifying alerts, and operational failures                                  |
| Completed-candle TA      | Run when a new required 15m, 1h, or 4h candle has completed                                  | One immutable snapshot per user/symbol/frame/candle/version                                         |
| Intrabar/event detection | Separate future model using live or forming data                                             | Label explicitly as intrabar; never mix with completed-candle fixtures                              |
| News ingestion           | Poll, webhook, licensed feed, or later stream with source timestamps                         | Store source identity, publication/event time, retrieval time, and immutable content reference/hash |
| Notification delivery    | Event-driven from a durable alert                                                            | Record attempts and retry transient failures without creating a new market alert                    |
| Outcome evaluation       | When the defined future horizon is available                                                 | Update or append a versioned outcome without rewriting the issued signal                            |
| Replay/backtest          | Explicit historical dataset and clock                                                        | Write a separate research run, never production alerts or baselines                                 |
| ML training              | Versioned feature/outcome dataset with chronological splits                                  | Store model, dataset, feature, and evaluation versions                                              |

A completed 4h strategy updating only at a new 4h close does not mean the product
waits four hours to detect a sudden geopolitical or market-wide event. The movement,
intrabar, and news paths cover that different problem. Completed-candle TA avoids
repainting and enables faithful replay; fast detection handles events before the
larger candle closes.

## Data and evaluation rules

- Record UTC event time, observation time, processing time, source, symbol mapping,
  interval, and calculation/model version.
- Keep completed and forming candles distinguishable throughout collection and storage.
- Never stitch candles from different exchanges into one analytical series.
- Use one authoritative writer for each state transition. Retries and overlapping
  schedules must be idempotent.
- Make unavailable, stale, partial, rejected, skipped, and failed states visible.
- Keep issued analysis immutable. Later outcomes attach evidence; they do not rewrite
  what the system originally reported.
- Backtests must use only information available at each simulated time and use the
  same source mapping, completion, gap, and freshness rules as live analysis.
- Evaluate chronologically on later data. Include fees, spread, slippage, funding,
  latency, and missing/delisted assets before interpreting simulated profitability.
- Establish simple baselines before ML. Logistic regression is a useful first model
  because its inputs and calibration can be inspected. More complex models must show
  better out-of-sample performance, not merely better training fit.
- Define metrics per claim: detection precision/recall and delay, forecast calibration
  and Brier/log loss, directional accuracy by horizon, and simulated strategy return
  after costs. A single generic "accuracy" number is insufficient.

## Operating constraints

- Free services can sleep, restart, throttle, or stop at quota boundaries. Every
  scheduled workflow needs explicit timeout, retry, idempotency, and missed-run behavior.
- The scheduled trigger is not proof that a run completed. Persist or expose enough
  status to distinguish invocation, partial work, success, and failure.
- Account ownership must survive every service boundary. Do not trust a caller-supplied
  user ID; verify identity and constrain database operations server-side or with RLS.
- Service-role/database credentials stay server-side. Prefer narrowly scoped RPCs or
  user JWTs over distributing a broad administrative key.
- Production cutovers must leave exactly one scheduled writer active.
- Market-data requests should be shared for identical symbol/source/timeframe inputs
  where doing so preserves account-specific rules. Batch database work when it keeps
  transactions and failure reporting clear.
- Cost optimization must not change analytical semantics silently. A lower-frequency
  or reduced-data strategy is a new version when it changes what the system can detect.
- Logs must identify safe stages and reason codes without storing tokens, user payloads,
  private baselines, response bodies, or credentials.

## Conditions for architectural changes

### Move more orchestration to Python/FastAPI when

- the deployed Python service has measured acceptable uptime, cold-start latency,
  bandwidth, and cost under the intended schedule;
- monitoring, news, replay, and analysis materially benefit from one Python workflow;
- authentication and account isolation have integration tests at the new boundary;
- Python has a narrow, reviewed persistence interface and durable failure records;
- one active writer, idempotency, deployment rollback, and scheduler cutover are defined.

One-language consolidation alone is insufficient. Ordinary browser CRUD can remain
direct-to-Supabase even if Python owns monitoring and analysis.

### Move persistence to Python/Django when

- the project chooses a directly accessible PostgreSQL database that it controls;
- Python is intended to own application schema migrations and transactions;
- Django ORM/admin benefits outweigh migration and operational costs;
- authentication and existing row ownership can be transferred and verified;
- data export, import, reconciliation, backup, and rollback have been rehearsed.

Django models can be created for the schema, but Django adds limited value while it
is only another HTTP client of a database whose schema and privileged operations are
owned elsewhere.

### Add Celery or another durable queue when

- jobs must survive HTTP request limits or process restarts;
- independent concurrency, delayed retries, priorities, or worker pools are needed;
- news processing, notification delivery, model training, or backtests interfere with
  latency-sensitive live monitoring;
- operating a worker and broker is justified and monitored.

### Add a persistent WebSocket collector when

- a defined detector needs latency below practical REST polling;
- measurements show repeated REST downloads or provider limits are a real bottleneck;
- reconnect, sequence-gap recovery, heartbeats, and completed-candle construction are
  specified and tested;
- the collector feeds the same validated analysis functions used by replay.

### Change hosting when

- quotas, sleep behavior, request duration, database access, or cost repeatedly violate
  measured service objectives;
- the replacement has a tested backup, monitoring, security-update, and recovery plan;
- the migration provides a concrete capability rather than a framework preference.

## Delivery stages

1. **Current foundation:** authenticated dashboard, persisted watchlists/settings,
   cumulative movement alerts, scheduled/manual monitoring, TA v2 history/outcomes,
   and an optional read-only Python analysis path.
2. **Operational efficiency:** measure current requests/writes, prevent overlapping
   work, fetch identical market inputs once, run completed-candle TA only when due,
   batch safe persistence, and retain useful failure/status evidence.
3. **Canonical Python analysis:** separate calculation from transport, port TA v2
   faithfully, prove fixed-fixture parity, verify the hosted scheduled path, switch
   the saved-signal pipeline, and retire the production TypeScript calculator.
4. **Replay and outcome evidence:** replay historical candles through the same Python
   logic, expand clearly defined outcome horizons, and report results by symbol,
   timeframe, source, and strategy version.
5. **News and fundamental context:** add source adapters, deduplication, event timing,
   relevance classification, citations, and optional AI summaries. Correlation with
   movement is evidence; it is not automatically causation.
6. **Combined research:** define versioned hypotheses using movement, TA, regime,
   participation, news, and macro inputs. Compare them with simple baselines.
7. **ML research:** begin with interpretable models such as logistic regression,
   chronological validation, calibration, and drift monitoring. Promote no model
   solely from backtest performance.
8. **Decision and delivery features:** add email and later WhatsApp delivery, plus
   hypothetical entry/TP/SL and leverage-aware risk scenarios with fees, funding,
   invalidation conditions, and recorded outcomes. Trading remains manual.

Each stage is independently useful. Later features do not justify weakening data
quality, provenance, account isolation, or failure visibility in earlier stages.

## Deployment and cost options

This is a planning comparison as of 2026-09-18, not a commitment or price guarantee.
Usage charges, taxes, backups, domains, monitoring, and third-party data/API plans can
change the totals. Check the linked provider pages before acting.

| Plan                                          | Stack                                                                              |        Approximate base cost | Reason to choose it                                                                              |
| --------------------------------------------- | ---------------------------------------------------------------------------------- | ---------------------------: | ------------------------------------------------------------------------------------------------ |
| A. Current development                        | Lovable Free + Render Free + current database/jobs                                 |         $0 within allowances | Continue development with no migration while measuring real usage                                |
| B. Convenient personal deployment             | Lovable Pro + Render Free                                                          |              About $25/month | Preserve current hosting, auth, database, and job management                                     |
| C. Paid Python API                            | Lovable Pro + the $7 Render service tier previously shown in the project dashboard |              About $32/month | Remove Python free-service sleep/compute constraints without moving the rest of the backend      |
| D. Independent free development               | Python web backend on Render Free + personally owned Supabase Free                 |         $0 within allowances | Gain an independently managed database project and normal connection options; requires migration |
| E. Independent personal deployment            | Python web app + worker/scheduler + PostgreSQL on a 2 GiB DigitalOcean VM          | About $12/month plus backups | Predictable infrastructure control and the ability to share one VM across processes              |
| F. Independent compute with managed data/auth | 2 GiB DigitalOcean VM + Supabase Pro                                               |         From about $37/month | Control Python execution while keeping managed PostgreSQL/Auth                                   |

Plan C's $7 covers one Render service, not a separate worker, Celery broker, database,
or backup system. A 2 GiB VM in Plan E is an initial low-concurrency estimate; live
monitoring, PostgreSQL, workers, news processing, and large backtests can exceed it.
Its disk also contains the operating system and application. Render-managed service
memory and total VM memory are not equivalent capacity promises.

Current reference limits and prices:

- [Lovable pricing](https://lovable.dev/pricing): credits cover building, Cloud, and
  in-app AI; all plans currently include a monthly Cloud grant. Hosting/database/jobs
  must not be treated as independently unlimited.
- [Render pricing](https://render.com/pricing) and
  [free-service limits](https://render.com/docs/free): Hobby currently includes 5 GB
  outbound bandwidth; excess bandwidth can be billed when a payment method exists.
  Free web services sleep after inactivity and are intended for hobby/testing use.
- [Supabase pricing](https://supabase.com/pricing): an independently owned Free project
  currently includes a 500 MB database; Pro starts at $25/month with 8 GB disk and
  compute credits covering one Micro instance. Free and Pro backup/pausing behavior
  differs and must be reviewed before migration.
- [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets): current
  Basic examples include 1 GiB/25 GiB at $6, 2 GiB/50 GiB at $12, and 4 GiB/80 GiB
  at $24 per month; backups cost extra.

A future static React deployment on a service such as Cloudflare Pages or GitHub Pages
is possible after browser/server boundaries are explicit. It does not itself replace
authentication, the database, scheduled work, or the Python runtime.

## Decision rule for future issues

Before implementing a feature, classify it:

- **Presentation or browser lifecycle:** React/TanStack UI.
- **User-owned CRUD:** Supabase with user JWT and RLS unless a later architecture
  decision moves the application data API.
- **Pure market calculation or model feature:** shared Python package.
- **Live HTTP exposure of Python logic:** FastAPI adapter.
- **Atomic durable state transition:** PostgreSQL transaction/RPC under one writer.
- **Collection or orchestration:** current TanStack server or a measured Python worker,
  selected explicitly without duplicating writers.
- **Offline research:** Python runner importing the same analysis package.
- **Notification delivery:** asynchronous adapter consuming a durable alert.

If a feature crosses several categories, keep the calculation core pure and make the
transport, authorization, persistence, and delivery boundaries visible. Update this
document when the decision changes; do not infer architecture from whichever framework
happens to contain the current implementation.
