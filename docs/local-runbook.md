# Local runbook: the forward-test MVP on one machine

Everything runs from one command. `npm run dev:local:all` starts local Supabase (app DB and the separate
operational DB), the Python API, the Binance collector, the hourly forward job (signals and paper
trading), the daily trend job and the app. `npm run mvp:check` says whether it is healthy.

## First time

1. **Reset both local databases** (the forward schema is edited in place, so an old DB is stale):
   ```sh
   supabase start && supabase db reset --local
   supabase start --workdir operational-db && supabase db reset --local --workdir operational-db
   ```
2. **Generate the local env files:**
   ```sh
   npm run env:local
   npm run env:local:operational
   ```
3. **Start everything without the forward jobs** (so the history is seeded first, with visible progress):
   ```sh
   npm run dev:local:all -- --no-forward
   ```
4. **Sign up** in the app (the URL Vite prints) with the account that will own the paper trading. Every
   registered account gets its own paper run; `FORWARD_USER_ID=<uuid>` in `.env.local` narrows the jobs to one.
5. **Seed the 1m history once** (about 130 days x 6 symbols, public Binance REST, a few minutes; it prints
   `[forward-backfill] BTCUSDT 37/125 ...` per page and ends with the days stored and minutes missing):
   ```sh
   npm run forward:backfill
   ```
   Re-running it only fetches what is still missing, so an interrupted run resumes.
6. **Restart with the forward jobs:** stop with Ctrl+C, then `npm run dev:local:all`.
7. **Check:** `npm run mvp:check` (exit code 1 on any FAIL, `--json` for machine output).

## A green `mvp:check`

Every line is `PASS` except possibly a few `WARN`s in the first days. In particular: collector health
`6 symbols x 1/15/60/240m LIVE`; per symbol `history` >= 120 days, `gaps 120d` 0 and `gaps 48h` 0; `forward
run status` ok and `age` under 2 h; `trend run` ok; `paper ledger` reconciles; both `append-only` lines
PASS. `records` shows signals, setups and final outcomes once the job has been running for a while.

## Daily routine

1. `npm run mvp:check`. If it is all PASS you are done.
2. Open the Strategy Lab: results are judged on finished trades only, with the random-timing baseline beside each
   strategy. A good-looking row is a hypothesis for the next sample, not an edge.

## What each WARN or FAIL means

| Line | Meaning and fix |
|---|---|
| `collector health` FAIL | A symbol/timeframe is not LIVE. Is the collector running (`dev:local:all` prints `collector`)? Right after a start it needs a few minutes; Binance outages show as `RECOVERING`. |
| `<SYMBOL> history` FAIL/WARN | Fewer than 60 (FAIL) / 120 (WARN) days of 1m candles. Run `npm run forward:backfill`; it fills the leading range. |
| `<SYMBOL> gaps 48h` FAIL | A missing minute in the last 48 h: the engine records `skipped_stale` until it leaves the window or is repaired. The hourly job repairs gaps from REST by itself; `npm run forward:backfill` does it now. |
| `<SYMBOL> gaps 120d` WARN/FAIL | Older gaps reach Python as missing bars (harmless); more than 120 missing minutes means the collector was down for long stretches. |
| `forward run status` FAIL `skipped_stale` | See the reason: `collector missing` (collector not LIVE), `gap(s)` (see above), `no candles` (empty history). |
| `forward run age` WARN/FAIL | No hourly run for 2 h (WARN) / 6 h (FAIL): the job is not running. Start `dev:local:all` (or `npm run forward:run`). |
| `backfill` WARN | `backfill_failed: ...` in the last run, usually HTTP 418/429 from Binance. It resumes on the next run; wait a few minutes. |
| `funding_unavailable` (in a run reason) | The funding fetch failed; windows that could contain a funding time stay open instead of being finalised with zero funding. It fixes itself on a later run. |
| `records` WARN | Signals but no setups: the volatility estimate (sigma) needs 60 days of completed windows. The count of `no_sigma` signals is shown. Nothing is filled; it clears as history accumulates. |
| `placebo controls` WARN | A strategy fired without its matched `placebo-v1:` control in the last 7 days. Normal at low signal rates; investigate only if it persists for weeks. |
| `paper ledger` FAIL | The wallet no longer reconciles (`initial + sum(amount) != last balance`) or `seq` has a hole. Stop the job and report it: the ledger is append-only and must never be edited. |
| `candle conflicts` WARN | Conflicts older than 24 h without a revision. Reconcile runs hourly in the collector; remaining ones are outside the tolerance or have no REST side and need a look. |
| `append-only` FAIL | A protection trigger is missing: the DB is not on the current schema. Reset it (step 1). |
| `python service` FAIL | The Python API is not reachable (`PYTHON_ANALYSIS_URL`); `dev:local:all` starts it. |

## Stopping

Ctrl+C in the `dev:local:all` terminal stops the app, collector and both jobs together (if one exits, the
launcher stops the rest and says which). `npm run dev:local:stop` also stops the local Supabase containers.

## Forward concurrency, evidence and retries

**Movement timeout:** the observed 1,036,800-row forward request took about 23 seconds while CPU work ran inside async handlers, blocking the single service event loop; the GIL can also delay other calculation threads. Calculation routes now use FastAPI's threadpool, while `/health` stays trivial and async. `dev:local:all` starts a separate forward process on the analysis port + 1 (override with `PYTHON_FORWARD_URL`), retaining `--limit-concurrency 32` on both processes. This isolates forward CPU work without splitting the process-local movement sessions across workers. Both processes stop with the launcher. The forward-evaluation timeout is 60 seconds; movement remains 6 seconds. Movement timeout errors identify the endpoint and elapsed milliseconds, and say “service busy or unavailable” when a bounded `/health` probe also fails. Standalone jobs outside the launcher should set the same `PYTHON_FORWARD_URL`; without it they retain the existing shared-service fallback.

**Day-clustered report:** for each strategy and matched control, group final trades by UTC calendar day of exit, with day sum `s_d`, day count `n_d`, total trades `n`, and trade-weighted mean `mean = sum(s_d)/n`. The report uses `SE = sqrt(sum_d((s_d - n_d * mean)^2))/n`, without a small-sample multiplier. The difference uses a Welch-style `z = (mean - control_mean)/sqrt(SE^2 + control_SE^2)`, not a paired-day test: day coverage and counts can differ, and dropping unmatched days or reweighting control days would change the reported trade-weighted comparison. Cross-series covariance and dependence across different exit days remain limitations. Both samples need at least 30 trades and 10 distinct exit days before comparison, with the existing z=2 floor and the two-sided 5% Bonferroni critical value over displayed rows. Days accompany trades in the table and results CSV; the old independence-based SE is labelled “naive SE” in CSV only. No verdict establishes a validated edge.

**Hourly run keys:** apply the new report and retry migrations through the normal migration workflow. `skipped_stale` attempts and all-symbol `no_sigma` attempts with no setups, resolutions or ledger events remain append-only audit rows but do not take the unique hour claim or advance the processed watermark. A later run in that same hour can succeed; after success, a repeat returns `already_done`. Partial evaluations and healthy runs with no new signals still consume the hour key. Existing historical rows are not rewritten. The wallet card reads the latest successful snapshot even when a newer attempt was skipped.

The wallet permits several positions per coin, keyed by setup; rejected entries already persist as zero-amount ledger events with reasons. Strategy Lab now shows each position and the latest 20 recorded rejections. Wallet v2 reserves the closing taker fee alongside the flat maintenance margin when selecting the largest integer leverage with liquidation distance at least twice the stop distance, capped at 20×. Entry fees are charged outside isolated margin. A tight stop can hit that cap; the card states when it does. Existing positions retain their recorded liquidation prices; only new positions use the fee reserve. These remain simulation assumptions, not exchange-tier guarantees.
