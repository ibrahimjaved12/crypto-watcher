# Historical market replay — Issue #35 Part 1

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

This is the market replay foundation, not complete Issue #35. Historical
provider adapters, technical-assessment composition, conditional setup and
outcome replay, funding-event replay, timestamped news, ordering-sensitive
execution evidence, and more efficient persisted checkpoint adapters are
deferred. OHLC/range replay for ATR remains separate under #111; portfolio
backtesting remains under #38. The existing Issue #17 TA smoke replay is
unchanged.
