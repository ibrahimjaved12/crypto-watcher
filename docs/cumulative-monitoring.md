# Cumulative movement monitoring

The alert rule now tracks net price movement from a saved baseline. Small moves
remain part of that comparison even when they take longer than 15 minutes.
The dashboard's rolling 5m/15m/1h/4h/24h figures are still available separately.

With threshold 2%:

| Observed close (USDT) | Result |
| --- | --- |
| 100 | First check establishes baseline; no alert |
| 101 | +1% from baseline; keep 100 |
| 101.99 | +1.99%; keep 100 |
| 102 | +2%; save upward alert, reset baseline to 102 |
| 102 | 0% from new baseline; no repeated alert, even after cooldown |
| 99.96 | -2% from 102; downward alert is eligible despite recent upward alert |

This is net endpoint movement, not addition of absolute fluctuations or of rounded
percentages. For example 100 → 101 → 99 → 100 ends at zero net change. It is also
not a trailing-high drawdown detector: a rise from 100 to 101.99 and fall to 100
does not trigger this 2% rule. Capturing swings from local peaks/troughs is a separate rule.

## State and execution

- The first fresh completed one-minute close seeds each account/pair's baseline.
  No historical baseline is inferred on migration deployment.
- Subsequent observations calculate `(price - baseline) / baseline * 100` in
  PostgreSQL NUMERIC. The threshold comparison cross-multiplies before rounding;
  exact +2% and -2% are included. Alert percentages are stored to four places.
- After successfully saving an alert, reset to its actual observed price. A large
  jump generates one alert, not a fabricated alert for every intervening 2% step.
- Cooldown is per account/pair/direction (default 15 minutes). Exact expiry is
  eligible. Suppression retains the baseline, so further net movement remains
  eligible on a **new observation** after expiry. No delayed-alert queue exists:
  a move that has reversed below threshold before expiry will not alert later.
- Source or threshold changes establish a new baseline on the next newer close,
  preserving directional cooldown times. This avoids comparing different venues'
  prices. Frequent fallback changes can therefore interrupt accumulation.
- Pausing preserves the baseline; resuming measures net change across the pause.
  Removing/re-adding the watchlist entry clears state through a foreign key cascade.
- Stale/unavailable input leaves state unchanged. Older or equal observation times
  are ignored, including retries after cooldown. A recovered feed can compare its
  fresh endpoint with the old baseline across a data gap; the rule does not assert
  anything about the unobserved path.

`src/lib/monitor/engine.server.ts` collects data and calls
`process_cumulative_observation`. The database transaction locks the watchlist row
and baseline, re-reads settings, evaluates cooldown, inserts the alert and advances
state atomically. Failed writes roll back all state changes. Baselines are
readable only by their owner and writable only through trusted server privileges.
Deleting a historical alert does not reset cooldown. The existing scheduler and
manual entry points use the same path; there is no additional scheduler.

`src/lib/monitor/observation.ts` selects the newest completed 1m close, honors
provider completion flags and rejects duplicate, malformed, future or more than
10-minute-old observations. Bad data tries the next provider. Dashboard quote
calculations remain the prior rolling implementation, including their documented
partial-candle limitations; monitoring no longer uses those rolling values.

Python's `market_analysis.cumulative.observe` implements the matching pure state
transition for replay and future backtesting. It assumes one supplied state per
account/pair, validated completed input and enabled monitoring. The app does not
use Python to write alerts: the atomic database function still enforces the rule.
The optional [FastAPI integration](python-api.md) now calls the same Python
evaluator for read-only dashboard previews, discarding proposed state changes.
No Django, authentication replacement or database migration to another platform
is involved.

## UI and stored alerts

Settings now describes the saved baseline instead of offering a fixed comparison
window. The old settings `window_minutes` column is retained for compatibility but
is ignored by the new alert engine. Historical alerts keep their original windows.
New alerts have `comparison_mode = 'baseline'`, null `window_minutes`, baseline
price/time and observation time. History displays the baseline comparison and CSV
includes these new fields. `triggered_at` is the processing time, which can differ
from the completed candle's observation time.

## Deployment and verification

Apply `supabase/migrations/20260915090000_cumulative_monitor.sql` through the normal
Lovable Cloud migration process **before deploying this app revision**. It adds
state and alert metadata, with no changes to existing scheduler registration or
auth configuration. The new monitor fails explicitly if the RPC is missing; it
does not silently run the previous algorithm. No live migration or deployment was
performed during implementation. The new behavior starts only after migration
and app deployment; existing accounts seed on their first fresh check.

Use the existing single scheduled monitor. Verify the actual job's target URL and
run logs at deployment; the reported preview target remains unverified locally.
Drain in-flight checks during cutover to avoid overlapping old and new code.

Local checks:

```sh
npm ci --prefix tests --ignore-scripts
npm test --prefix tests
node node_modules/typescript/bin/tsc --noEmit
cd python
python3 -m unittest discover -s tests -v
```

The test-only PGlite dependency is pinned separately and creates an in-memory
PostgreSQL database, with a minimal auth schema, then executes the real migrations.
No live connection or credentials are used. Tests cover threshold boundaries,
net movement, retries, directional cooldown, source/rule resets, rollback, pause,
ownership and permissions. PGlite serializes queries; simultaneous multi-connection
contention must still be checked in staging, although row locks provide database
serialization. Unit tests separately validate completed-candle selection.

Validation: 13 local JavaScript/database tests and 32 Python tests passed;
TypeScript typecheck passed. App build remains blocked before compilation by the
environment's Node 21.7.1; installed Vite/Rolldown require Node ^20.19.0 or >=22.12.0.
