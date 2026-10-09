# Implementation and deployment roadmap

> **Provisional.** This document is AI-generated and is not the source of truth. The owner's
> intent in [`CLAUDE.md`](../CLAUDE.md) takes precedence. Reconciliation is tracked in
> [#181](https://github.com/ibrahimjaved12/crypto-watcher/issues/181).

Snapshot: 2026-10-09, reset under [#189](https://github.com/ibrahimjaved12/crypto-watcher/issues/189)
and [#181](https://github.com/ibrahimjaved12/crypto-watcher/issues/181). Order numbers are
the `Roadmap order` values in the current issue bodies (#31 = 19, #32 = 20, #38 = 26,
#43 = 31). This supersedes the earlier eight-stage roadmap. See [product direction and architecture](product-direction.md)
for the futures-only product, current/target diagrams, evidence rules, and ownership.
See [analysis records and evaluation](analysis-evaluation.md) for histories,
conditional setups, scores, outcomes, and strategy research. See
[futures paper-trading simulation](futures-simulation.md) for automated virtual
wallet execution, accounting, historical runs, and live paper trading.

## Current priorities (CLAUDE.md section 3, set 2026-10-07)

1. **Benchmark and research first.** Choosing and testing predictive strategies runs
   through the stop-aware benchmark: [#180](https://github.com/ibrahimjaved12/crypto-watcher/issues/180)
   (strategy research), [#182](https://github.com/ibrahimjaved12/crypto-watcher/issues/182)
   (harness, labels, trial ledger) and [#183](https://github.com/ibrahimjaved12/crypto-watcher/issues/183)
   (dataset and frozen six-symbol universe). These are not numbered below: they are the
   gate in front of the strategy-dependent orders, and research itself happens in chat.
   Frozen universe: BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT, DOGEUSDT, XRPUSDT; horizons
   15m, 1h, 4h; long and short always.
2. **Then the MVP setup, outcome and ledger work:** #31 setups (order 19), #32
   outcomes (20), #33 reports (21), then the paper-trading wallet and costs (24, 25)
   and portfolio backtests (26). A setup is a benchmark barrier event and carries a
   `validated`/`unvalidated` status; see [analysis-evaluation.md](analysis-evaluation.md).
   Signal-level edge is decided by the benchmark, not by #38.
3. **Parked, do not work on unless asked:** news ingestion (#29, order 17), email
   (#40, order 28) and WhatsApp (#44, order 32), plus other backlog polish. Their
   order numbers are kept for reference only. ML (#43, order 31) starts only once
   enough labeled events from strategies worth filtering exist.

Orders are a dependency-aware reference, not a work queue: the priorities above
decide what is worked on next.

## Status and dependency rules

**Current** means implemented in repository code: dashboard/auth/CRUD, Binance USDⓈ-M REST
monitoring, Python TA history and forward returns, temporary workload controls,
and manual Python analysis. It does not confirm live scheduler health.
See [activity controls](activity-controls.md) for the current switches. **Proposed**
means agreed incremental work below, including external operational storage and
public futures streaming. **Conditional** means an option requiring evidence,
including paid hosting, Django, Celery, direct Python database ownership, or full
backend migration.

Ready/Backlog below are the issues' planning labels, not completion claims. Order
matches the recommended dependency-aware roadmap order, not issue number. The local
and staging foundation (#47–#53 and #57, orders 33–39) starts alongside the scope
and measurement work; it must not wait for the product backlog. The dependency column distinguishes
implementation prerequisites from measurement, deployment, and closure gates.
Design can overlap, but each production cutover must satisfy its gates. Later
integration requirements are called out rather than pretending that all
dependencies already exist.

## Ordered issue map

| Order | Issue and deliverable                                                                                                                                                   | Planned status | Dependencies and gate                                                                        |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------- | -------------------------------------------------------------------------------------------- |
| 1 | [#12 — Update product scope, futures architecture, and deployment roadmap](https://github.com/ibrahimjaved12/crypto-watcher/issues/12)                                  | Ready          | None; records the agreed direction.                                                          |
| 2 | [#13 — Verify monitoring execution and establish a measured runtime-cost baseline](https://github.com/ibrahimjaved12/crypto-watcher/issues/13)                          | Ready          | #12; measure before claiming savings.                                                        |
| 3 | [#22 — Add activity controls for polling, scheduled monitoring, and TA work](https://github.com/ibrahimjaved12/crypto-watcher/issues/22)                                | Ready          | Implementation may proceed; #13 comparable baselines gate savings and closure claims.        |
| 4 | [#14 — Optimize market collection, calculations, and database persistence](https://github.com/ibrahimjaved12/crypto-watcher/issues/14)                                  | Ready          | #13 and #22 measurement baselines; preserve movement semantics.                              |
| 5 | [#15 — Separate market collection, analysis, movement alerts, and notification controls](https://github.com/ibrahimjaved12/crypto-watcher/issues/15)                    | Backlog        | #14 is not an implementation blocker; its measurements and regression checks gate closure.   |
| 6 | [#16 — Add overlap protection, restart recovery, and freshness reporting](https://github.com/ibrahimjaved12/crypto-watcher/issues/16)                                   | Backlog        | #14–#15 contracts; establish ownership/recovery now, complete stream tests with #26.         |
| 7 | [#23 — Make market-data providers and instrument identities strictly futures-only](https://github.com/ibrahimjaved12/crypto-watcher/issues/23)                          | Backlog        | Futures identity cutover; runtime overlap/recovery hardening continues in #16.               |
| 8 | [#17 — Port TA v2 into a shared deterministic Python calculation package](https://github.com/ibrahimjaved12/crypto-watcher/issues/17)                                   | Backlog        | #23 input contract; prove fixed-fixture TA parity.                                           |
| 9 | [#18 — Extend Run Python analysis to cover the intended futures-analysis workload](https://github.com/ibrahimjaved12/crypto-watcher/issues/18)                          | Backlog        | #17; exercise representative futures analysis through the read-only bridge.                  |
| 10 | [#19 — Run a bounded Python analysis trial and evaluate persistent-worker hosting](https://github.com/ibrahimjaved12/crypto-watcher/issues/19)                          | Backlog        | #18; bounded API trial and separate persistent-worker hosting assessment.                    |
| 11 | [#24 — Persist immutable analysis conclusions, trade setups, and provenance](https://github.com/ibrahimjaved12/crypto-watcher/issues/24)                                | Backlog        | #23 and #17; immutable provenance before scheduled cutover.                                  |
| 12 | [#20 — Switch scheduled TA to Python and retire the TypeScript TA calculator](https://github.com/ibrahimjaved12/crypto-watcher/issues/20)                               | Implemented    | Shared Python calculation; TanStack remains the sole writer.                                 |
| 13 | [#25 — Introduce external PostgreSQL for frequent operational updates and selective result synchronization](https://github.com/ibrahimjaved12/crypto-watcher/issues/25) | Backlog        | #13 baseline, #16 ownership/recovery, #24 records, and #20 boundary.                         |
| 14 | [#26 — Implement a shared futures WebSocket collector with REST bootstrap and gap recovery](https://github.com/ibrahimjaved12/crypto-watcher/issues/26)                 | Backlog        | #19 hosting evidence, #23 futures feeds, #25 allocation; complete #16 gap tests.             |
| 15 | [#27 — Define retention, historical-data storage, and verified archival policies](https://github.com/ibrahimjaved12/crypto-watcher/issues/27)                           | Backlog        | #24–#26 data inventory; verify archives and restores before cleanup.                         |
| 16 | [#28 — Add market-wide futures movement and acceleration detection](https://github.com/ibrahimjaved12/crypto-watcher/issues/28)                                         | Backlog        | #23 and #26 fresh shared market events.                                                      |
| 17 | [#29 — Add news ingestion and a scheduled-event timeline](https://github.com/ibrahimjaved12/crypto-watcher/issues/29)                                                   | Backlog        | #24 provenance and #27 retention; permitted source access.                                   |
| 18 | [#30 — Present current assessments and developing futures opportunities on the dashboard](https://github.com/ibrahimjaved12/crypto-watcher/issues/30)                   | Backlog        | #28–#29 assessments; consume structured setups when #31 lands.                               |
| 19 | [#31 — Implement versioned conditional futures-trade setups and scoring](https://github.com/ibrahimjaved12/crypto-watcher/issues/31)                                    | Backlog        | #17 shared core, #24 provenance, #28–#29 required strategy inputs.                           |
| 20 | [#32 — Track setup lifecycles and evaluate trade outcomes in event order](https://github.com/ibrahimjaved12/crypto-watcher/issues/32)                                   | Backlog        | #31 fixed strategy rules and #24 immutable records; integrate detailed liquidation with #37. |
| 21 | [#33 — Add performance reports with date, strategy, and score-band filters](https://github.com/ibrahimjaved12/crypto-watcher/issues/33)                                 | Backlog        | #32 outcomes and #24 versions; wallet metrics follow #36–#38.                                |
| 22 | [#34 — Acquire and validate historical futures datasets for replay](https://github.com/ibrahimjaved12/crypto-watcher/issues/34)                                         | Backlog        | #23 contract semantics and #27 storage/retention; preserve point-in-time metadata.           |
| 23 | [#35 — Build historical replay using the shared strategy and analysis engine](https://github.com/ibrahimjaved12/crypto-watcher/issues/35)                               | Backlog        | #17 shared core, #31–#32 event rules, and #34 validated history.                             |
| 24 | [#36 — Build a futures paper-trading wallet and execution ledger](https://github.com/ibrahimjaved12/crypto-watcher/issues/36)                                           | Backlog        | #31–#32 setup/transition contracts; basic fills first, exchange fidelity in #37.             |
| 25 | [#37 — Model exchange-specific futures costs, execution constraints, and liquidation](https://github.com/ibrahimjaved12/crypto-watcher/issues/37)                       | Backlog        | #23 effective contract rules, #34 history, and #36 accounting.                               |
| 26 | [#38 — Add reproducible backtest experiments and portfolio comparisons](https://github.com/ibrahimjaved12/crypto-watcher/issues/38)                                     | Backlog        | #33–#37 reports, replay, wallet, and execution/cost models.                                  |
| 27 | [#39 — Run continuous live paper trading through the shared simulation engine](https://github.com/ibrahimjaved12/crypto-watcher/issues/39)                              | Backlog        | #26 live recovery plus #35–#38 shared simulation and execution rules.                        |
| 28 | [#40 — Deliver email notifications with independent preferences, deduplication, and retries](https://github.com/ibrahimjaved12/crypto-watcher/issues/40)                | Backlog        | #15 preferences, #24 events, #16 recovery; consume setup/paper events as available.          |
| 29 | [#41 — Review Python orchestration and a fully external backend using operational evidence](https://github.com/ibrahimjaved12/crypto-watcher/issues/41)                 | Backlog        | #13, #19, #25–#27, #29, and #38–#40 operational evidence.                                    |
| 30 | [#42 — Add AI-assisted strategy review and controlled improvement experiments](https://github.com/ibrahimjaved12/crypto-watcher/issues/42)                              | Backlog        | #33 and #38 reproducible experiments; explicit review before promotion.                      |
| 31 | [#43 — Evaluate an ML baseline against the existing strategy rules](https://github.com/ibrahimjaved12/crypto-watcher/issues/43)                                         | Backlog        | #31 rule baseline, #34–#38 datasets/experiments; chronological holdouts.                     |
| 32 | [#44 — Add WhatsApp delivery and channel-specific notification preferences](https://github.com/ibrahimjaved12/crypto-watcher/issues/44)                                 | Backlog        | #40 reliable email/outbox foundation; official-provider and consent review.                  |
| 33 | [#47 — Prevent local development from targeting hosted services by default](https://github.com/ibrahimjaved12/crypto-watcher/issues/47)                                 | Ready          | Establish the safe local/hosted environment boundary; foundation work starts in parallel.    |
| 34 | [#48 — Make the database reproducible with the local Supabase CLI](https://github.com/ibrahimjaved12/crypto-watcher/issues/48)                                          | Ready          | #47; finish deterministic bootstrap, users, RLS/RPC smoke tests, and CI reset coverage.      |
| 35 | [#50 — Provide reliable local authentication without Lovable Cloud](https://github.com/ibrahimjaved12/crypto-watcher/issues/50)                                         | Ready          | #48 reproducible local database and Auth services.                                           |
| 36 | [#49 — Add a safe local monitoring runner and end-to-end cycle test](https://github.com/ibrahimjaved12/crypto-watcher/issues/49)                                        | Backlog        | #47, #48, and #50; require loopback targets and mocked market data.                          |
| 37 | [#51 — Restore one green offline local verification command](https://github.com/ibrahimjaved12/crypto-watcher/issues/51)                                                | Backlog        | #47–#50 local paths and #57 diagnostics-test repair.                                         |
| 38 | [#52 — Configure optional Google OAuth integration testing](https://github.com/ibrahimjaved12/crypto-watcher/issues/52)                                                 | Backlog        | #50 local auth boundary; email/password remains the default.                                 |
| 39 | [#53 — Provision a shared external Supabase staging environment](https://github.com/ibrahimjaved12/crypto-watcher/issues/53)                                            | Backlog        | #47 and #48; keep staging explicit and isolated from production.                             |
| — | [#57 — Fix the three Python-analysis diagnostics test failures](https://github.com/ibrahimjaved12/crypto-watcher/issues/57)                                             | Ready          | Repair the current offline suite before #51 consolidates it into one verification command.   |

## Stages and completion gates

1. **Scope and measured baseline (1–2):** document the direction and verify actual
   scheduler/browser activity and runtime cost. Issue #13 owns live verification;
   its observed disabled cron is not changed by documentation work.
2. **Safe local and staging foundation (33–39 and #57, parallel):** isolate local development
   from hosted services, reproduce the database and Auth stack, add a safe local
   monitoring cycle, repair the diagnostics-test regression, consolidate offline
   verification, and keep OAuth and shared staging explicit. This stage begins
   alongside stage 1 and must not wait for the product backlog. No default command
   may access staging or production.
3. **Controlled current operation (3–6):** control development polling, remove
   measured waste, separate activity controls, and establish concurrency/recovery
   contracts. Preserve existing alerts and directional cooldown semantics. #13 is
   a measurement and closure gate for #22, not an implementation blocker. #14 is a
   measurement/regression gate for closing #15, whose separation work may proceed.
4. **Futures and canonical calculations (7–12):** futures identity and the shared
   Python calculation path are present. TanStack remains the one scheduled writer;
   the movement monitor remains independently owned until explicitly migrated.
5. **Operational data and collection (13–15):** finalize table ownership, add
   external PostgreSQL with transactional outbox and selective Lovable sync, prove
   authenticated fresh-state reads, then deploy the persistent shared collector.
   Test disconnect/restart, bounded REST recovery, overlap deduplication, and visible
   freshness. Measure storage/WAL growth and verify archival restore before cleanup.
6. **Assessments and conditional opportunities (16–21):** add market-wide movement,
   readable assessments, structured versioned setups (benchmark barrier events with a
   validated/unvalidated status), ordered lifecycle outcomes in R units net of costs, and
   reports. News/calendar context (#29, order 17) is parked until unparked by the owner. The assessment UI can ship before the setup
   engine but cannot present placeholder opportunities as working strategies.
   Earlier outcome reports must identify missing wallet/exchange-cost capabilities.
   Follow the [analysis and evaluation specification](analysis-evaluation.md).
7. **Replay and virtual execution (22–27):** acquire point-in-time futures datasets,
   replay the shared engine, build constrained wallet accounting, validate fees,
   funding, fills and liquidation, run reproducible portfolio comparisons, and then
   drive the same simulation engine with live public streams. Wallet, automation,
   cost, and event-order rules are defined in the
   [paper-trading simulator specification](futures-simulation.md). Real-money execution stays manual for now (CLAUDE.md section 2); no real orders are placed.
8. **Delivery and evidence-based evolution (28–32, email and WhatsApp parked):** review
   architecture from operational evidence and run controlled AI/ML research (ML only
   if it beats simple baselines out of sample); email (#40) and WhatsApp (#44) wait
   until unparked. Research can
   recommend versioned changes; it cannot silently rewrite live strategy rules.

## Closure review checkpoints

These notes do not replace the live issue bodies or close an issue automatically:

- **#12:** the repository's acceptance-evidence table below is complete. Confirm the
  live issue has no additional acceptance item, then close it.
- **#22:** the activity controls and focused tests are implemented. Treat #13's
  comparable runtime/cost measurements as a separate closure gate only when the live
  issue requires measured savings; do not keep implementation work blocked on them.
- **#15:** the independent controls, pause/resume semantics, migration, and tests are
  present. Apply and verify the migration in the intended environment, complete the
  #14 regression/measurement gate, then close it if the live issue adds no requirement.
- **#47:** local/hosted target validation and environment tests are present. Confirm
  the live issue has no remaining hosted-boundary scenario, then close it.

## Deployment and cost decisions

The immediate baseline retains Lovable as the main application database and
TanStack orchestration, with the optional FastAPI service. The proposed increment
adds independently hosted persistent collection and external operational data; it
does not require moving authentication or all application data. External PostgreSQL
does not host the collector process, and browser tabs are not its runtime.

Before choosing hosting in #19/#25, compare uptime/sleep behavior, region access to
public futures data, memory, CPU, outbound bandwidth, database connections,
backups, storage, worker needs, and maintenance. Compare equal dated usage windows,
separating Lovable Database server, storage, Compute, and Network. Moving writes
does not prove that an active Lovable database becomes free. Do not infer prices
from row counts or treat a single web-service price as the cost of workers, broker,
database, backups, and monitoring combined.

The earlier approximate monthly stack totals are retired: this roadmap makes no
current price quote or free-tier capacity promise. A paid service, VM, managed
database, or fully external backend is conditional on measured requirements and a
dated comparison. Record the selected plan, limits, budget, rollback, and actual
deployment state in the implementing issue. No deployment is performed by #12.

## Dated provider references

Reference date: **2026-09-20**. Recheck at implementation time; no quota or pricing
value here is a permanent guarantee.

| Provider/reference                                                                                                             | Planning implication                                                                                                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [Binance USDⓈ-M API and limits](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info) | Futures REST uses `fapi.binance.com`; inspect endpoint weights, exchange-info limits, and response headers, and back off on throttling. Verify stream-specific connection/subscription rules when selecting streams in #26. |
| [Render free-service limits](https://render.com/docs/free) and [pricing](https://render.com/pricing)                           | Free web services can spin down after inactivity. A successful on-demand API trial does not establish suitability for a permanent collector; assess worker hosting separately.                                              |
| [Supabase Edge Function limits](https://supabase.com/docs/guides/functions/limits) and [pricing](https://supabase.com/pricing) | Hosted functions have bounded execution duration. Use them only for bounded work; independently assess external database capacity, backups, pausing, and connections.                                                       |
| [Lovable pricing](https://lovable.dev/pricing)                                                                                 | Obtain a dated plan/Cloud usage breakdown during the cost baseline; builder subscription and ongoing backend consumption must be evaluated separately.                                                                      |
| [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets)                                                  | A possible conditional persistent-host comparison, including backups and maintenance; no VM size or monthly total is selected here.                                                                                         |

## Issue #12 acceptance evidence

| Acceptance criterion                                                                       | Documentation evidence                                                                                                                                                            |
| ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Conditional opportunities and net profitability                                            | [Product purpose](product-direction.md#product-purpose-and-boundaries) and [lifecycle](product-direction.md#product-lifecycle)                                                    |
| Futures-only Binance USDⓈ-M scope                                                          | [Product purpose](product-direction.md#product-purpose-and-boundaries) and [futures evidence contract](product-direction.md#futures-semantics-and-evidence-contract)              |
| Separate assessment, setup, activation, management, evaluation, backtesting, paper trading | [Lifecycle](product-direction.md#product-lifecycle) and [glossary](product-direction.md#glossary-and-record-boundaries)                                                           |
| Separate current and proposed diagrams                                                     | [Current architecture](product-direction.md#current-architecture-and-data-flow) and [proposed architecture](product-direction.md#proposed-incremental-architecture-and-data-flow) |
| Explicit service/database responsibilities                                                 | [Ownership table](product-direction.md#service-and-database-ownership)                                                                                                            |
| News/event context and limits                                                              | [News and event context](product-direction.md#news-and-event-context)                                                                                                             |
| Current/intended histories, setups, scores, outcomes, and research                         | [Analysis records, conditional setups, and evaluation](analysis-evaluation.md)                                                                                                    |
| Virtual-wallet automation, accounting, costs, and paper runs                               | [Futures paper-trading simulation](futures-simulation.md)                                                                                                                         |
| Revised roadmap links to issues                                                            | [Ordered issue map](#ordered-issue-map), covering all 40 roadmap entries (orders 1–39 plus unnumbered #57)                                                                                                          |
| Documentation-only change                                                                  | README and documentation updates only; no runtime, secret, schedule, or infrastructure changes                                                                                    |

## Strategy specifications

- [SPEC-0 master plan](strategy-specs/SPEC-0-master-plan.md)
- [Strategy catalogue](strategy-catalogue.md)
