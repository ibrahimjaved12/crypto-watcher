# Independent activity controls

Issue #15 separates permission to obtain market input from permission to run each
consumer of that input. The controls are user-scoped and live in Monitoring settings.
They apply to both scheduled checks and **Run check now**. Dashboard **Refresh** and
the read-only **Run Python analysis** action remain explicit browser actions; neither
is a background monitoring run.

## Current controls

| Website control                     | Stored field                     | Default | Current effect                                                                                                                                                                                             |
| ----------------------------------- | -------------------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Monitoring master switch            | `monitoring_enabled`             | On      | Off skips the user's monitoring run and removes that account's contribution to the shared collector universe. Individual choices and saved state remain unchanged. |
| Market-data collection              | `market_data_collection_enabled` | On      | In collector mode, On includes the account's watched symbols in the shared universe. Off removes that account's contribution and pauses its consumers that require collection. Other eligible watchers can keep a symbol assigned. |
| Movement-alert generation           | `movement_alerts_enabled`        | On      | Off skips completed-one-minute observation processing, baseline advancement, cooldown checks, and alert insertion.                                                                                         |
| Completed-candle technical analysis | `completed_candle_ta_enabled`    | On      | Off skips new 15m/1h/4h TA snapshots and current pending TA-outcome evaluation.                                                                                                                            |

When collector mode is enabled, TanStack reads account watchlists/settings, derives
the shared subscription universe, and assigns it to the operational database. The
independent persistent worker reads that assignment and owns Binance public
WebSocket collection plus REST bootstrap/recovery. Current collector health and
bounded completed-candle history live in the operational database; the worker never
reads Lovable watchlists/settings. See [collector ownership](binance-futures-collector.md).

Disabling collection or removing the final eligible watcher removes the symbol
from the shared universe. Assignment atomically makes its current health
`UNAVAILABLE` with reason `collection disabled/unsubscribed`, and the worker
unsubscribes when it reconciles. Late health writes cannot restore active health
outside the assigned universe. Historical completed candles remain subject to the
existing retention policy. Re-enable assigns the symbol again but leaves its health
unavailable until the worker establishes current evidence through normal
bootstrap/recovery.

In legacy request-driven mode, the checkpoint database function re-reads the master
and collection switches. The movement database function also re-reads the movement
switch while holding
their transactional locks. This prevents an already-started request from advancing
state or saving an alert after a concurrent pause. TA and provider work already in
flight may finish; user-setting changes govern the next unit of orchestration and do
not attempt unsafe cancellation of an external request.

Deployment environment flags remain higher-level operational safeguards:

- `SCHEDULED_MONITOR_ENABLED=false` prevents authenticated scheduler invocations
  from starting any user's run; it does not modify user preferences.
- `TA_GENERATION_ENABLED=false` and `TA_OUTCOME_EVALUATION_ENABLED=false` can stop
  their respective TA operations even when the user's TA control is on.
- Browser `VITE_*` refresh flags only control automatic display queries and do not
  alter any saved monitoring preference.

See [temporary activity controls](activity-controls.md) for those deployment flags.

## Pause, resume, and catch-up

| Activity                | While paused                                                                                | On resume                                                                                                                                                                                                                                 |
| ----------------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Shared collector (enabled) | The account stops requiring the symbol. If no eligible watcher remains, assignment makes current health `UNAVAILABLE` and the worker unsubscribes. Historical completed candles remain retained. | Reassignment allows normal worker bootstrap/recovery; health becomes `LIVE` only after current evidence is re-established. |
| Legacy REST collection | No monitoring provider request or checkpoint advancement. | A new run requests fresh data and atomically advances only to a newer completed candle. It does not invent the missing intraperiod path. |
| Movement alerts         | The saved baseline, last observation, and directional cooldown timestamps remain unchanged. | The next fresh completed one-minute close is compared with the preserved baseline, so net movement across the pause can alert. There is no queue of one alert per missed threshold crossing.                                              |
| Completed-candle TA     | No new snapshot and no pending outcome update.                                              | The latest completed candle can create one current snapshot; missed signals are not backfilled. Pending outcomes are retried from available provider history and become unavailable when their required candle can no longer be obtained. |

Removing a watchlist pair is not a pause: its movement baseline is deleted by the
existing foreign-key cascade. Historical alerts and TA records retain their existing
retention behavior.

## Future controls and notification channels

The schema reserves `developing_setup_evaluation_enabled` and
`paper_trading_enabled`, both defaulting to off. The Settings page labels these as
unavailable and does not offer working switches. No strategy evaluator, virtual
wallet, fill, or order path was added by issue #15.

`notification_channel_preferences` stores one fail-closed preference per user and
channel (`email` or `whatsapp`). It is separate from event creation: pausing delivery
must never stop an alert, assessment, setup transition, or paper-trading ledger event
from being saved by its authoritative owner. There is currently no delivery adapter,
outbox, recipient configuration, or Settings toggle, so changing a database
preference cannot send a message.

The implementing delivery issues must use channel-specific durable outbox state,
stable event IDs, idempotent attempts, bounded retries, expiry, and dead letters.
Resuming a channel may deliver only still-eligible pending messages under a recorded
policy; it must not present old catch-up events as fresh market or trade alerts.

Future setup evaluation and paper trading need independent durable event-time cursors.
They must replay missed inputs in order and within a bounded recovery policy before
processing new decisions. Paper execution must not skip directly to the newest price
when doing so could hide an entry, stop, target, funding event, or liquidation. Those
runtime rules belong to the setup and simulator issues, not this control foundation.

## Deployment

Apply `supabase/migrations/20260921090000_activity_domains.sql` before deploying the
application revision. The migration preserves existing behavior by enabling the
three current domains and disabling the two future engines. It also replaces the
movement RPC with the transaction-level independent pause checks, adds the
RLS-readable/service-written REST checkpoint, and creates the RLS-protected
notification preference table. No scheduler, new provider, delivery service, or
real/paper execution is enabled by the migration.

For collector mode, recreate the disposable pre-release operational database from
`operational-db/supabase/migrations/20261004000000_operational_schema.sql`, the single
current baseline containing the subscription-aware health RPC.
See the [operational reset policy](binance-futures-collector.md#pre-release-operational-database-reset).
