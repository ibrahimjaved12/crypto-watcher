# Historical market replay — Issue #35

This Python replay kernel consumes a caller-supplied, validated Binance USD-M
trade dataset, completed one-minute movement candles, explicit source-state
intervals, a fixed universe, and an output interval. It does not download data,
access a database, use the wall clock, or reproduce movement formulas.

For each five-second movement boundary `t`, the processing clock is `t +
finalization_grace_ms` (2,000 ms by default). A trade can enter that boundary
only after its dataset `first_seen_at_ms` and when its trade time is at most `t`.
The emitted canonical #71 evaluation remains timestamped at `t`. In replay,
`MarketObservation.received_at_ms` means the dataset first-seen availability
analogue; it does not claim to reconstruct an original WebSocket receive time.
Trades arriving after a bucket finalized are passed to canonical #70 for its
late-observation rejection and are never used to revise an earlier point.

One canonical `MovementBucketEngine` runs per configured symbol. Every boundary
from the derived warm-up start through the output end advances once, including
quiet periods. Source state must explicitly cover every symbol and boundary;
absence of trades never implies an outage or `LIVE`. The warm-up start is the
output start minus `(WINDOW_BUCKETS[15] - 1) * BUCKET_INTERVAL_MS` and
`MAX_LAST_TRADE_AGE_MS`. Raw ordered trade/aggTrade records are required; candles
are never converted into synthetic five-second trades.

Historical candles become visible only after their completed minute and their
first-seen time. Canonical `build_historical_window_inputs` retains its strict
prior rule: a candle ending at or after evaluation `t` cannot enter `t`'s
historical reference distribution. Canonical `calculate_market_movement` then
produces #71 evidence from the finalized buckets and visible candle history.

A run fingerprint hashes the dataset manifest, fixed universe and instrument
contract, movement version and parameters, output interval, grace, and replay
policy. Point IDs hash that fingerprint with each movement boundary. Research
partition cutoffs are separate from this identity. The pure
`to_market_state_experiment_points` export labels existing #71 evaluations and
source-time evidence as development, validation, or test without recalculation;
Issue #75 experiment runners can consume the resulting chronological stream.

Checkpoints use **rebuild-and-skip v1**: resume validates the fingerprint and
point ID, rebuilds canonical engine state from warm-up, then emits only points
after the checkpoint. No private engine state is serialized. Trade processing,
late-rejection, source-state, and market-wide eligibility counts remain visible
in replay diagnostics.

## Part 2: verified local Binance USD-M daily archives

`binance_historical_archive.py` builds a Part 1 `HistoricalReplayRequest` from
locally present **Binance public-data USD-M futures daily** `aggTrades` and
`1m` kline ZIPs. It derives exact UTC dates from the replay configuration and
opens only these paths under the supplied `archive_root`:

```text
data/futures/um/daily/aggTrades/<SYMBOL>/<SYMBOL>-aggTrades-YYYY-MM-DD.zip
data/futures/um/daily/klines/<SYMBOL>/1m/<SYMBOL>-1m-YYYY-MM-DD.zip
```

Each ZIP must have a sibling `.zip.CHECKSUM` with one SHA-256 entry naming that
ZIP. The adapter verifies its bytes before opening it, checks ZIP member paths,
and parses the single expected CSV member without extraction. Missing ZIPs or
checksums fail dataset assembly. V1 uses daily packages only: it does not fall
back to monthly archives, discover symbols from folders, or download files.
The configured universe order and USD-M perpetual instrument IDs are preserved.

The archive `aggTrades` timestamp is mapped exactly to `event_time_ms`,
`trade_time_ms`, and `first_seen_at_ms`. This
`exchange-timestamp-surrogate-v1` policy approximates exchange-time causal
availability. **The archive does not preserve the original WebSocket event
time or socket receive time, so archive replay cannot reconstruct historical
receive latency.** No arbitrary latency is added. Part 1 still finalizes a
boundary at `t + finalization_grace_ms`. A completed `1m` candle first becomes
available at its `close_time + 1`, equal to `open_time + 60,000` milliseconds.

Under `verified-archive-coverage-live-v1`, every required package must verify
before the adapter emits one continuous `LIVE` source interval for each symbol.
Here `LIVE` means **verified archive coverage for the requested interval**. It
does not assert that the original historical WebSocket collector was healthy
at every instant. Missing local packages, quiet trading, aggregate-ID gaps,
and gaps in genuine archived klines are not converted into `STALE`,
`RECOVERING`, or `UNAVAILABLE` source evidence. Kline gaps remain unfilled and
are counted in diagnostics.

The bundle manifest records relative archive paths and verified ZIP SHA-256
values. Its content SHA-256 hashes the adapter/dataset versions, the two
policies, and sorted path/hash pairs, independent of the local root path. A
corrected archive ZIP therefore changes the dataset identity and Part 1 run
fingerprint. The adapter returns this manifest, factual diagnostics, and a
ready-to-run `HistoricalReplayRequest`; pass that request directly to
`run_historical_market_replay`, then optionally export chronological Issue #75
points with `to_market_state_experiment_points`.

Issue #35 still does not include technical-assessment composition, conditional
setup and outcome replay, funding/event/news adapters, execution resolution,
or a batch runner. Replay-facing OHLC/range evidence for ATR remains separate
under #111; portfolio simulation remains under #38. The existing Issue #17 TA
smoke replay is unchanged.
