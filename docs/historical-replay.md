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
Replay keeps a per-symbol cache of the shared builder's result for one run.
Reuse requires both an unchanged visible-candle generation and unchanged
aligned eligible end ranges for 1m, 5m, and 15m under the exact lookback and
strict-prior rules. Newly visible candles, including late older minutes, and
lookback or strict-prior transitions cause the shared builder to run again.
This cache has no wall-clock expiry and does not change replay identity.

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

## Part 3: fixed historical experiment batch

`historical_experiment_batch.py` loads one verified Part 2 dataset, runs Part 1
market replay once, and exports one immutable development/validation/test point
stream. It passes that same stream and one shared canonical classifier and
lifecycle configuration to all **28** preregistered runs: three configurations
each for EXP-75-01, 02, 03, 04A, 04B, 05, 06A, 07, and 08, plus one for
EXP-75-09. The batch requires the default canonical `MarketMovementConfig()`
and at least two configured symbols because EXP-75-08 rejects a one-symbol
universe. Runs execute sequentially; HMM training unavailability is retained
as a native scientific diagnostic. Broken experiment contracts fail the batch.

The compact report records archive and replay manifests/diagnostics, suite and
configuration identities, each experiment's native summaries for development,
validation, test, and all points, and HMM training diagnostics/model SHA when
available. It does not store per-point experiment histories or choose a winner.
An experiment-stream SHA binds replay point IDs to partition labels and cutoffs.
The batch SHA binds that stream to the dataset, replay, suite, universe, and
classifier/lifecycle configuration. Run IDs hash the batch SHA with the
experiment and config identities. Each result SHA hashes the compact native
summaries and parameters. The report SHA hashes canonical JSON **without**
the `report_sha256` field; the final JSON then includes that digest. Identical
inputs produce identical bytes without wall-clock or absolute-path data.
Optional `code_revision` appears in report metadata and its report SHA, but is
excluded from the scientific batch identity.

For local archives already present under the Part 2 directory layout:

```bash
python -m market_analysis.historical_experiment_batch \
  --archive-root /path/to/binance-public-data \
  --symbols BTCUSDT ETHUSDT \
  --universe-id research-top2 \
  --universe-version v1 \
  --start 2026-08-01T00:00:00Z \
  --end 2026-08-08T00:00:00Z \
  --development-end 2026-08-05T00:00:00Z \
  --validation-end 2026-08-07T00:00:00Z \
  --output-json report.json
```

CLI timestamps must be explicit UTC seconds (`Z`) on five-second boundaries;
symbol order is preserved. The CLI emits only canonical JSON plus one newline
to stdout when no file is specified. File output uses an fsynced temporary
sibling and atomic replacement. An existing report requires `--overwrite`.
The complete suite is fixed; the CLI offers no experiment filtering or
parameter tuning. The CLI flushes archive, replay, coarse boundary, experiment
`N/28`, and elapsed-time progress to stderr. Direct library calls remain quiet
unless given a progress callback. Timing and progress stay out of reports and
all scientific fingerprints.

## Part 4: official archive acquisition and cache

`binance_historical_download.py` explicitly fetches the exact daily USD-M
`aggTrades` and `1m` kline ZIPs required by Part 2 from
`https://data.binance.vision/`. It uses Part 2's date and path helpers, keeps
configured symbol order, and does not crawl remote directories. V1 is
sequential and daily-only, with no monthly or REST fallback.

Each online run fetches the official `.CHECKSUM` first, even for cached ZIPs.
The downloader reuses a local ZIP when its SHA-256 matches that fresh remote
digest and repairs any missing or stale local checksum file. When the official
digest changes, it streams the replacement ZIP into a temporary sibling file,
checks SHA-256, stages a normalized checksum, then atomically replaces the ZIP
and checksum in that order. A failed transfer or digest mismatch leaves an old
ZIP untouched. A crash between the two replacements can leave a mismatched
pair; the next acquisition repairs it. Part 2 remains the final semantic and
integrity gate after all files are present, and it leaves checksum-valid but
malformed archives available for investigation.

V1 uses a 30-second per-request timeout and up to three attempts for transient
HTTP/transport failures, with fixed one- and two-second retry delays. HTTP 404
reports a missing required official archive; permanent 4xx errors, malformed
checksums, and completed ZIP checksum mismatches fail without retry. Download
results distinguish `DOWNLOADED`, `REUSED`, and `REFRESHED`; byte counts cover
ZIP body bytes, and per-item attempts count checksum plus ZIP HTTP requests.
These network mechanics are not scientific identity. **Part 2's verified ZIP
content hashes continue to define the dataset identity**, independently of
cache location, retries, or download timing. Parts 2 and 3 still work offline
against previously cached archives without invoking this tool.

```bash
python -m market_analysis.binance_historical_download \
  --archive-root /path/to/binance-public-data \
  --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT DOGEUSDT \
  --universe-id research-pilot \
  --universe-version v1 \
  --start 2026-08-01T00:00:00Z \
  --end 2026-08-03T00:00:00Z
```

The CLI accepts explicit UTC (`Z`) five-second boundaries, uses the canonical
movement configuration and a 2,000 ms finalization grace by default, and
prints a compact acquisition summary to stdout. It does not launch the Part 3
experiment batch; run that command separately when ready to evaluate the
cached dataset.

Issue #35 still does not include technical-assessment composition, conditional
setup and outcome replay, funding/event/news adapters, or execution resolution.
Replay-facing OHLC/range evidence for ATR remains separate under #111;
portfolio simulation remains under #38. The existing Issue #17 TA smoke replay
is unchanged.
