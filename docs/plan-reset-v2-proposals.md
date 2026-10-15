# Plan reset v2: ticket proposals

> **Proposal, written by AI.** Everything in this file is a proposal written by AI (not an
> instruction and not a decision). `CLAUDE.md` is the owner's intent and takes precedence.
> Nothing here edits, closes or relabels a ticket. The owner decides each row; a later pass
> applies the decisions. Tracking: [#340](https://github.com/ibrahimjaved12/crypto-watcher/issues/340)
> (successor of [#181](https://github.com/ibrahimjaved12/crypto-watcher/issues/181)).

Basis. `[read]` = the ticket body (and the comments cited) was read in full for this pass.
`[triage]` = judged from its title, labels and the notes in #340, #181 and #364 only; its body is
**not** re-read, so the proposal must be checked against the body before it is applied.

Actions: **rewrite** (change the body or title), **close**, **keep**, **park** (keep open, label
`status: parked` or `status: paused`, no work).

## A. Tickets the owner's plan-reset list calls "open" but that are closed

The owner's instruction for CLAUDE.md section 7 lists #49, #51 and #41 as "still open". On 2026-10-13
all three were closed. They are listed here so the owner can decide whether to reopen, rewrite or
leave them closed.

| Ticket | State | What is stale or contradictory | Proposed action | Proposed replacement (2-3 lines) | Why |
| --- | --- | --- | --- | --- | --- |
| #49 | closed 2026-10-13 | Plan A/B for a local runner built on Lovable cron and Lovable-hosted scheduling. | keep closed | If a local runner is wanted, it is a one-shot check on the machine's worker host with mocked exchange data. Repeat mode stays off by default. | v5 runs checks on the machine through the runner gateway; no Lovable cron exists. `[read]` |
| #51 | closed 2026-10-13 | Asks for a local verification command. CI (`verify.yml`) now runs the checks, and CLAUDE section 6 says GitHub checks cover them. | keep closed | No separate ticket: CI is the verification path. If local Supabase RLS/RPC checks are still wanted, they belong in #48. | Duplicates CI. `[read]` |
| #41 | closed 2026-10-13 | Written around Lovable, Django/Celery and a fully external backend. v5 decided the shape (CLAUDE sections 5 and 8). | keep closed; optional new ticket | Review after 30 days of v5 operation: measured machine uptime, collector gaps, worker cost. No migration decision before then. | The options it compares are settled or rejected in CLAUDE. `[read]` |

## B. Open tickets

| Ticket | State | What is stale or contradictory | Proposed action | Proposed replacement (2-3 lines) | Why |
| --- | --- | --- | --- | --- | --- |
| #27 | open, narrowed 2026-10-13 | The narrowed text is right. The collapsed "original text" still describes bulk research archives and a Lovable-era layout. Its acceptance points at `docs/operational-database.md`, which still describes the Lovable layout (see #282). | rewrite (delete the collapsed original; keep the narrowed scope) | Retention for the application stores: hosted Supabase, the machine's operational DB, paper and outcome ledgers. Per class: owner, hot retention, archive rule, deletion condition, written in `docs/operational-database.md` for v5. | The original text would bring back research-data scope that #183 owns. `[read]` |
| #34 | open, narrowed 2026-10-13 | The narrowed text is right. The collapsed original still describes a greenfield archive downloader, which #183 now owns. | rewrite (delete the collapsed original) | Close only the execution-evidence gaps the data lake (#183) does not cover: contract and bracket history, stop-vs-target ordering, manifest consistency. | Avoids a second downloader. `[read]` |
| #123 | open, paused | Its body contradicts CLAUDE section 4 in four places: its own 10/8/12 split by selected dates (owner split: development 2024-01..2025-06, validation 2025-07..2025-12, hidden 2026-01..2026-09); a five-symbol universe (six are frozen); primary horizons 1m/5m/30m/1h (primary horizons are 15m/1h/4h); Tardis data. | park (keep paused); rewrite only if revived | If revived, it runs under CLAUDE section 4: six frozen symbols, primary horizons 15m/1h/4h, owner splits. Until then the 2026-10-03 Part-C freeze is history only. | Four contradictions with the owner's intent. The owner decides whether to close. `[read]` |
| #152 | open, paused | Blocks #123 only. Metadata is inconsistent: `state_reason` is "reopened" while its closing PR #156 is merged. | park; close with #123 | Parked with #123. If #123 is revived, first confirm the merged #156 state and the current memory bound. | Nothing to do until the #123 decision. `[read]` |
| #163 | open, paused | Proposes temporarily making the repo public for hosted compute, with Tardis-based inputs. Conflicts with CLAUDE section 4 (private research-data repo; paid data needs the owner's OK) and section 6 (public repo, no private notes). Refers to a Discussion that was never published. | park; owner decision on visibility and data sources | GitHub Actions batches for the research lake (#183) run from the private research-data repo. No repository visibility change and no paid data without the owner's decision. | Visibility and paid data are owner decisions. `[read]` |
| #164 | open, labelled priority high / backlog | The owner deprioritized it on 2026-10-09 (recorded in #181), but the labels were not changed. References #162, which is not open. | park; set the deprioritized label | Deprioritized until #13 runtime measurements exist (decision 2026-10-09). Then audit the machine worker host and the Python research code. | Labels contradict the decision. `[read]` |
| #165 | open, labelled priority high / backlog | Built on premises that v5 replaced: Render hosting and free-tier limits, a FastAPI movement service on a Uvicorn worker, Lovable-era writes. The movement engine now runs on the owner's machine and is published through the worker host (#372). The cited commit is old. | rewrite or close; park with #164 | Reduce live movement-state transport between the machine's operational DB and the worker-host publisher (#372), keeping the five-second cadence and Decimal semantics. | Several premises are gone. `[read]` |
| #180 | open, labelled status ready | The header still says the horizon rule is an open decision; the owner withdrew it on 2026-10-16 (#340). Points to #181 (closed) for the reset. The header says IN PROGRESS while the label says ready. | rewrite (header only) | Status 2026-10-16: in progress. Horizon rule unchanged (CLAUDE section 4). Plan reset tracked in #340. | Stale status. `[read]` |
| #30 | open, backlog | The current-assessment part was delivered in #372 (Refs #30, merged 2026-10-15). The body still asks for levels, news and setups in the same ticket. | rewrite scope; keep open | Developing-opportunity view on the dashboard, after #31 versioned setups exist. Current assessment is delivered (#372). Remaining: levels and the news/event timeline. | Keeps the ticket honest about what is left. `[read]` |
| #299 | open, backlog | The pattern snapshot and movement-alert setup charts are delivered (#370, #371). Remaining: trade overlay and equity renderers, not wired (`docs/alert-charts.md`, "Not done here"). | rewrite; park until paper trading (#36) | Trade overlay and equity-curve renderers, fed by paper-trading data, once #36 exists. | The current body is mostly done. `[read]` |
| #298 | open | The movement-alert hosted viewer is delivered (#371, Refs #298). Pattern charts still have no hosted copy or viewer. | rewrite scope | Hosted viewer for pattern-event setup charts (owner-only signed URLs), if the owner still wants it. The movement-alert viewer is done (#371). | Scope partly delivered. `[docs]` (body not re-read) |
| #68 | open, backlog | Lovable-era: provisioning an external operational Postgres, a "Lovable serverless" runtime, staging in #53. CLAUDE section 5 says external operational Postgres is no longer planned; the collector runs on the machine (#90 closed). | rewrite or close | Collector resource caps (`BINANCE_COLLECTOR_*` bounds) and restart/reconnect checks on the owner's machine. No external operational database. | Contradicts CLAUDE section 5. `[read]` |
| #50 | open, labelled ready | Lovable Google OAuth broker and "Lovable Cloud paused" framing. v5 uses the owner's own Supabase. | rewrite | Email/password auth against the local Supabase and the owner's hosted project. Google OAuth is optional and separate. | Lovable-era framing. `[read]` |
| #279 | open, backlog | Assumes forward records move into the local operational DB, and that the hosted project is Lovable-era. Overlaps with the data lake (#183, private repo). The backup part is still valid. | rewrite to backups only | Backups and a tested restore for the operational DB and the hosted project's permanent tables. Research data is owned by #183. | Overlap with #183. `[read]` |
| #224 | open | Includes "start a forward liquidation recorder". CLAUDE section 4 defers liquidations, and any paid purchase needs the owner's OK; a recorder is new collection and needs an explicit owner decision. | park; owner decision | Metrics archive (open interest, long/short ratios) only. The liquidation recorder waits for the owner's decision. | Conflicts with the "liquidations deferred" rule. `[triage]` |
| #349 | open | Liquidation archive acquisition. CLAUDE section 4: liquidations deferred, no free contiguous source, a paid purchase needs the owner's OK. | park; owner decision | Liquidation evidence: deferred (CLAUDE section 4). Any acquisition, free or paid, needs the owner's explicit OK. | Conflicts with section 4. `[triage]` |
| #35 | open | #34's text treats #35 Parts 1-4 as already landed. Body not re-read. | check; close or rewrite | Remaining replay work on the shared engine after the #34 narrowing. Close if Parts 1-4 already cover the rest. | Possibly done. `[triage]` |
| #36 | open, backlog | Owner decision 2026-10-16: paper trading stays in the backlog until a few strategies survive costs. | park | Parked until a few strategies survive costs in the benchmark (owner decision 2026-10-16). | CLAUDE section 3. `[triage]` |
| #38 | open, backlog | Same owner decision. | park | Parked with #36. Reproducible backtests resume once strategies survive costs. | CLAUDE section 3. `[triage]` |
| #39 | open, backlog | Continuous live paper trading; same owner decision. | park | Parked with #36. | CLAUDE section 3. `[triage]` |
| #239 | open | The MVP-1 local harness includes a paper wallet and a minimal dashboard; same owner decision. | park | Parked with #36. Forward evidence comes from the machine collector and the benchmark (#182). | CLAUDE section 3. `[triage]` |
| #308 | open, no labels | Stable runner hostname. The always-on host decision (2026-10-13) says it is not needed during development; revisit at about 80% completion. | park | Revisit with the always-on host decision at about 80% completion. | Decision recorded in #340. `[triage]` |
| #297 | open | Tiered candlestick detection. Pattern work was paused by the owner on 2026-10-16. | park | Parked with the doji work (#364). | `[triage]` |
| #301 | open | Cross-check against TA-Lib and TradingView defaults; part of the paused pattern tree. | park | Parked with #361 and #364. | `[triage]` |
| #361 | open | Parallel candlestick detectors; the owner parked the pattern tree on 2026-10-16 (see the #364 comments). | park | Parked (owner, 2026-10-16). Resume only when the #364 resume order reaches it. | `[triage]` |
| #362 | open | Detector registry and TA-Lib adapter; same pause. | park | Parked with #361. | `[triage]` |
| #363 | open | Parallel doji comparison; same pause. | park | Parked with #361. | `[triage]` |
| #366 | open | Doji arming and execution variants. #364 says to resume only if a lead exists; none is recorded. | park | Parked; resume only if #364 finds a lead. | `[triage]` |
| #356 | open | Follow-ups from the #355 re-review of the paused pattern study. | park | Parked with #364. | `[triage]` |
| #364 | open | Doji tracking. The header still lists the Part 5 scope, which is done; the current status is in the comments. Paused by the owner on 2026-10-16. | rewrite header; add the `status: paused` label | Doji exploration: paused by the owner on 2026-10-16. Resume order: Task C, then Task E, then Task D (see the comments). | Header is out of date. `[read]` (comments) |
| #348 | open | "Pattern study framework v2 (doji first)". The doji-first scope is paused, but the candle table (daily/weekly and intraday) stays in use (CLAUDE section 4, 2026-10-15). | rewrite title and scope | Candle table as the shared base for pattern studies. Doji-first scope paused on 2026-10-16. | `[triage]` |
| #229 | open | "Commit the strategy specification library to docs/". `docs/strategy-specs/` and `docs/strategy-catalogue.md` already exist on main. | verify; close if done | Close if `docs/strategy-specs/` and `docs/strategy-catalogue.md` already hold the library. | Possibly done. `[triage]` |
| #228 | open | Multi-venue replication with OKX "usable from Pakistan". Could read as a second primary venue; CLAUDE section 5 makes OKX and Kraken fallbacks only, and #339 removes the Worker fallback. | rewrite | OKX and Kraken as fallback sources only (CLAUDE section 5). Binance stays primary. The Worker fallback is temporary (#339). | `[triage]` |
| #226 | open | Composition harness. Depends on a strategy list that is still research pending and owner-led (CLAUDE section 8). | park | Parked until the owner's strategy list (CLAUDE section 8) exists. | `[triage]` |
| #225 | open | Cross-asset family. Same dependency. | park | Parked with #226. | `[triage]` |
| #219 | open | Strategy catalogue and composition tracking. Same dependency. | park | Parked with #226. `docs/strategy-catalogue.md` stays as the reference. | `[triage]` |
| #282 | open | The Lovable-exit cleanup overlaps this docs pass (for example, `docs/operational-database.md` still describes the Lovable layout, per #27). | keep; narrow the scope | Cleanup of code, tests and dependencies after the Lovable exit. Docs are covered by the plan reset (#340). | `[triage]` |
| #340 | open | This tracking ticket. Its "Proposed CLAUDE.md edits" are superseded by the PR that carries this file; D1 was withdrawn on 2026-10-16. | rewrite body after merge; owner closes | Tracking for the plan reset. Remaining items: the ticket proposals in `docs/plan-reset-v2-proposals.md`. | Body is out of date. `[read]` |

## C. Open tickets checked, no change proposed

Triaged by title and labels only (`[triage]`); none looked stale or contradictory. #374 and #373
are new placeholders from 2026-10-16: #374 forex (owner-led, nothing scheduled); #373 market-wide
breadth research (research first). #222 is in the backlog (CLAUDE section 3). #31, #32 and #33 were
rewritten or are unaffected; the CLAUDE section 7 bullet about #31 is listed in the PR as a question for the owner.

#374, #373, #338, #339, #290, #288, #281, #271, #264, #222, #221, #220, #188, #187, #186, #184, #182,
#62, #48, #44, #43, #42, #40, #33, #32, #31, #29, #273.
