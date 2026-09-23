# Monitor efficiency and local measurements

Issue #14 is implemented and verified locally without enabling the Lovable Job or
using a hosted database. Production billing and scheduler verification remain part
of the paused issue #13 investigation; the measurements below do not claim a
Lovable credit saving.

## Durable processing contract

- One manual or scheduled invocation owns a request-scoped market-data context.
  Identical futures trade-price inputs are shared across accounts only when source
  policy, instrument, timeframe, and price type match. The cache is discarded after
  the invocation and never silently serves old data to a later run.
- Movement monitoring requests only 1m candles. The dashboard quote path retains
  its separate 1m plus 15m history because it needs rolling changes and chart data.
- TA first reads one compact due-work result for a pair. A timeframe with the
  current `ta-v2` completed candle and no due outcome performs no exchange request,
  calculation, upsert, or outcome write.
- A delayed runner recovers at most eight missing completed candles per timeframe
  per pass, oldest first. Every recovered candle has at least 200 consecutive bars.
  A gap older than the provider's 250-bar response fails explicitly instead of
  skipping history or treating stale data as current.
- Due outcome rows are selected in the database, capped at 500 per pair/timeframe, and their
  idempotent pending-to-final transitions are applied in one batch per timeframe.
- Run metrics are stored on `monitor_runs` with duration. No run history, alerts,
  or TA research records are pruned by this change. Metrics count exchange HTTP
  attempts, transferred candle rows, shared-cache hits, TA calculations, saved TA
  signals, updated outcomes, database reads, write attempts, and known no-op
  decisions. Provider failures remain visible and their attempted requests are counted.

The database migration is
`supabase/migrations/20260924090000_monitor_efficiency.sql`. It adds the metrics
columns and the service-role-only `get_ta_due_work` and `apply_ta_outcomes` RPCs.

## Deterministic before/after request model

The old happy path for one account and one pair downloaded two movement histories
(61 × 1m and 97 × 15m rows) plus three 250-row TA histories. Before provider
metadata, fallback, and outcomes, that was five candle requests and up to 908 rows.
It also attempted three TA calculations/upserts and made three separate pending
outcome reads on a repeated run.

| Same completed TA candle, no due outcome |              Before |               After |
| ---------------------------------------- | ------------------: | ------------------: |
| Candle HTTP requests                     |                   5 |                   1 |
| Requested candle-row limit               |                 908 |                  61 |
| TA calculations                          |                   3 |                   0 |
| TA snapshot upsert attempts              |                   3 |                   0 |
| TA due-work reads                        | 3 broad frame reads | 1 compact pair read |

The remaining candle request is the 1m movement observation. When new TA work is
due, the path uses one 1m observation series and one series for each due timeframe.
For multiple accounts watching the same pair in one scheduled invocation, those
identical exchange results are fetched once and reused; persistence remains
account-scoped.

These figures are a deterministic operation model backed by mocked provider tests,
not a live network or billing benchmark. Actual response row counts and fallback
attempts are recorded by the new run metrics.

## Local verification

The tests use stubbed exchange responses and PGlite with the real migrations. They
cover repeated-candle gating, the eight-candle catch-up bound, strict cache keys,
batched idempotent outcomes, service-role permissions, futures provenance, and the
existing movement baseline/cooldown behavior.

Run locally:

```sh
npm test --prefix tests
npx tsc --noEmit
npm run build
```

The full JavaScript command currently also reports the three pre-existing
Python-analysis diagnostics failures tracked by issue #57. The focused #14 checks
are `activity-controls.test.mjs`, `activity-domains.test.mjs`,
`cumulative.test.mjs`, `observation.test.mjs`, and `ta.test.mjs`.

Do not enable the Lovable Job to gather these local measurements. When issue #13
resumes, compare equal dated hosted windows and treat the saved counters as workload
evidence rather than a direct conversion to credits.
