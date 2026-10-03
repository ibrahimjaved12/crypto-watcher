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

Canonical five-second bucket membership uses `trade_time_ms` (Binance trade/transaction time `T`).
`event_time_ms` (event time `E`) and receive/first-seen timestamps remain provenance/diagnostic
evidence, not bucket identity. Live orchestration uses wall clock plus grace to decide when the
explicit trade-time boundary is safe to finalize; replay uses the deterministic processing clock
above, and Python owns permanent explicit-boundary finalization in both paths.

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

The legacy `HistoricalReplayCheckpoint` API remains **rebuild-and-skip v1**:
it validates the fingerprint and point ID, rebuilds from warm-up, and emits
only later points. The #152 study executor instead uses durable runtime
checkpoints containing exact `MovementBucketEngine` state, pending visible
trades, source position, cumulative diagnostics, and immutable JSONL point
chunks. Checkpoints are written at deterministic hourly output boundaries and
at output end. A restart validates the entire contiguous checkpoint chain and
restores the latest complete state; corrupt or conflicting files fail closed.
These files are local operational working state, not scientific observations.

The study's raw aggTrades are deduplicated and ordered in a bounded-cache,
disk-backed SQLite index, then streamed into the same canonical replay loop
used by materialized callers. Ordering is `(first_seen_at_ms, symbol_index,
trade_time_ms, numeric aggregate_trade_id)`. Final scientific outputs and
fingerprints retain their existing versions. Frozen `code_revision` continues
to identify the scientific producer; checkpoints separately bind the actual
`runtime_implementation_revision` from Git HEAD. Final period reports remain
create-only.

By default, checkpoints live under `<output-dir>/.runtime-checkpoints/` and
progress observations append to `<output-dir>/study-progress.jsonl` with
flush and fsync. `execute --checkpoint-dir PATH --progress-report PATH` can
override those locations. Progress and checkpoint files do not enter final
scientific report hashes.

## Archive research and live history

Official Binance historical archives can support reproducible causal research when each
symbol/date/partition's availability, checksum, schema and gaps are individually validated.
Historical aggregate trades are therefore recoverable for verified partitions. Archive data is
not an exact record of the original WebSocket receive stream: it does not reconstruct local
receive timing, delivery anomalies or reconnect history. Availability must be checked per partition.

[Issue #120](https://github.com/ibrahimjaved12/crypto-watcher/issues/120) used official Binance USD-M
archives for BTCUSDT, ETHUSDT, BNBUSDT, SOLUSDT and DOGEUSDT on 2026-09-19 and 2026-09-20:
10 checksum-verified aggTrade archives, 4,786,579 parsed archive rows and 1,202,462 causally processed
trades. [Issue #123](https://github.com/ibrahimjaved12/crypto-watcher/issues/123) uses the same
archive/manifest model for its frozen corpus. Research files live outside transactional PostgreSQL;
live state retains only bounded processing/recovery/current-consumer needs. This does not justify
long-term storage of every raw aggTrade or five-second bucket in PostgreSQL. See
[persistence proposal requirements](./market-movement-engine.md#operational-state-and-research-history).

The 2,000ms grace remains a versioned evidence-admission policy in the replay fingerprint, even
though it does not change the return formula.

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

## Auxiliary completed OHLC evidence for later ATR research (#111)

The same verified Part 2 kline load also retains immutable Binance USD-M
**trade-price** 1m open/high/low/close rows in `dataset.ohlc_evidence`. Each
finalized row carries its symbol and instrument, exact open/close timestamps,
Decimal prices, and `first_seen_at_ms = close_time_ms + 1`. The
`exchange-close-time-surrogate` basis is a deterministic archive replay
assumption, **not proof of actual historical network receipt**. The collection
has its own version and SHA-256 over sorted rows and the verified dataset
identity. Its digest is auxiliary: legacy replay, batch, and experiment hashes
do not include it.

Use `dataset.ohlc_evidence.as_of(symbol, "1m", t, limit=20)` with `t` taken
from a replay point's `evaluation_boundary_time_ms` to obtain candles in
open-time order. The query requires both completion and first-seen availability
by the point's boundary `t`, without
adding the movement replay's finalization grace. At 10:05:00, the 10:04
candle first seen at 10:05:00 is available to this OHLC query; an older
candle first seen at 10:05:01 is not, even though movement replay may process
the 10:05:00 boundary at 10:05:02. Canonical #71 historical normalization
also keeps its separate strict-prior rule and excludes a candle ending at
`t` from its reference samples.

Each returned candle includes the immediately preceding minute's close when
that exact minute is present, finalized, and available by `t`. Otherwise it
reports `MISSING_PRECEDING_MINUTE` or `PRECEDING_NOT_YET_AVAILABLE`; gaps are
never bridged. An empty query reports `NO_AVAILABLE_CANDLE`. The latest
candle's timestamps remain visible for a future consumer to assess age, with
no stale threshold or ATR calculation defined here. The result exposes the
verified dataset SHA so a consumer can compare it with the replay manifest.

## EXP-75-12 taker-flow evidence sidecar

`load_binance_usdm_historical_replay_dataset(...,
include_taker_flow_evidence=True)` optionally retains immutable, sparse 5-second
buy/sell quote-notional buckets and aggTrade row counts during the same
validated archive pass. The default is off, so the fixed 28-run batch does not
pay flow aggregation or memory costs. The sidecar digest binds its canonical
buckets to dataset ID/version/content SHA, ordered symbols, replay engine
start/output end, bucket rule, aggressor-side mapping, archive availability
basis, and finalization grace. Prefix sums answer 1m, 5m, and 15m windows
without rescanning archive history at each point.

Bucket assignment and admission match `MovementBucketEngine`: exact 5-second
boundaries close the bucket at that boundary, and a row is admitted only when
its event time is at or before the boundary and its first-seen time is at most
`boundary + finalization_grace_ms`. Admission at the grace boundary is
inclusive; a row that misses its bucket is rejected permanently. At point `t`,
queries sum bucket boundaries in `(t − w, t]`.

The public archive's `buyer_is_maker` flag identifies aggressor side. The buy
and sell counts are counts of Binance public `aggTrades` archive rows, not
counts of individual fills, orders, or constituent trades. An aggTrade row can
aggregate fills sharing a price and taking side within 100ms interval. Describe
the result as archived aggregate taker-side flow.

The public archive schema does not include the API's `nq` field, so this
evidence cannot separate RPI quantity from the archived buy/sell quantities.
An empty observed interval means no aggTrade rows were present in the verified
archive input; it does not prove that the exchange had no trades or that a live
collector was healthy. The archive event timestamp used for
`first_seen_at_ms` is a replay surrogate, not an observed network-receipt
timestamp. The replay cannot establish original delivery latency or collector
health. These outputs are contemporaneous diagnostics only, not net long/short
positions or directional predictions. They do not measure forward returns;
those belong to #123.

The separate report can be generated from local verified archives with:

```bash
python -m market_analysis.historical_taker_flow_extension \
  --archive-root /path/to/binance-public-data \
  --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT \
  --universe-id research-pilot \
  --universe-version v1 \
  --start 2026-09-01T00:00:00Z \
  --end 2026-09-01T12:00:00Z \
  --development-end 2026-09-01T06:00:00Z \
  --validation-end 2026-09-01T09:00:00Z \
  --output-json taker-flow-extension.json
```

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

## EXP-75-06B: separate ATR historical extension

The historical ATR extension loads one verified archive, performs one canonical
market replay, and exports one partitioned point stream. Its library runner
accepts those already prepared objects for later shared orchestration. A single
strict-visibility OHLC cursor supplies 30, 60, and 120-minute ATR-SMA
configurations; matching 06A configurations run on the same point tuple for
V1/06A/06B comparisons. The fixed 28-run batch above is unchanged and does not
include 06B.

The extension has its own suite and report versions. Its scientific fingerprint
binds dataset ID/version/SHA, OHLC evidence version/SHA and availability basis,
ordered universe, replay and exported-stream fingerprints, partition cutoffs,
classifier/lifecycle configurations, and 06A/06B config identities. A separate
`candidate_output_sha256` binds the complete chronological 06B output at every
point: all candidate evaluation windows (including symbol rows, breadth,
aggregates, and other fields), all classification fields, lifecycle state, and
transitions. It uses the canonical report serializer and remains a result
digest separate from the extension input identity. The compact report records
per-partition availability reasons, ATR and calibrated scales, 06A RMS,
individual and common-ready coverage, score/breadth/outlier/state differences,
and lifecycle/onset summaries calculated from each complete chronological
branch. It does not serialize all paired points or rank a candidate.
Code revision appears in report metadata and the final report SHA, while the
scientific fingerprint excludes it.

```bash
python -m market_analysis.historical_atr_extension \
  --archive-root /path/to/binance-public-data \
  --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT \
  --universe-id research-pilot \
  --universe-version v1 \
  --start 2026-09-01T00:00:00Z \
  --end 2026-09-01T12:00:00Z \
  --development-end 2026-09-01T06:00:00Z \
  --validation-end 2026-09-01T09:00:00Z \
  --output-json atr-extension.json
```

The CLI uses the canonical V1 movement configuration, explicit UTC five-second
boundaries, and the verified local archive loader. It records the current Git
HEAD as code revision by default; --code-revision can supply the exact revision
for a packaged checkout. Existing report files require --overwrite and are
written atomically. No network acquisition is performed.
The example uses five symbols because canonical V1 requires at least five
eligible symbols for market-wide classification.

ATR uses archive candles only when first_seen_at_ms is strictly before the
evaluation boundary and the candle minute ended strictly before that boundary.
Archive first-seen time is an exchange completion-time surrogate, not actual
historical network receipt. 06B may warm from pre-output archive OHLC; 06A
starts cold at the first output point. One 12-hour pilot and agreement with V1
cannot establish prediction or trading profitability.

## Issue #127: historical mark-versus-trade diagnostic

`historical_mark_trade_extension.py` is a separate, opt-in descriptive
extension. It reuses the verified trade-price OHLC evidence from #111 and loads
Binance USD-M daily mark-price 1m klines into a separate evidence path. Mark
archives use
`data/futures/um/daily/markPriceKlines/<SYMBOL>/1m/<SYMBOL>-1m-YYYY-MM-DD.zip`
and a sibling `.zip.CHECKSUM`. The loader verifies each ZIP's bytes before
parsing. Missing or invalid packages remain unavailable with package statuses
and reasons; invalid rows, duplicate timestamps, off-grid timestamps, and
missing valid minutes are reported separately. The report includes each
expected package, verified checksum, package row issues, and per-symbol
expected/observed minute counts, coverage ratios, missing-minute ranges, and
longest gaps. Per-symbol and all-configured-symbol ready coverage is summarized
for development, validation, test, and all minute points. These are measured
coverage values; the extension applies no pass threshold.
Package statuses distinguish `MISSING_PACKAGE`, `MISSING_CHECKSUM`,
`INVALID_CHECKSUM`, `CHECKSUM_MISMATCH`, `MALFORMED_ROW`,
`DUPLICATE_MINUTE`, `OFF_GRID_MINUTE`, `MISSING_VALID_MINUTE`,
`INVALID_ARCHIVE`, or `VERIFIED_COMPLETE`. A checksum proves byte integrity
only; it does not prove that an archive contains every expected minute.

The requested study interval remains the replay's `--start` through `--end`.
The manifest records those boundaries and the first/last exact minute-boundary
points. For the first eligible point it loads the preceding 16 one-minute
candles: a 15-minute close-to-close return needs the close at `t - 15 minutes`,
which is the candle opened at `t - 16 minutes`. Warm-up candles support the
first comparison and are not emitted as additional study points.

The diagnostic emits only where a replay evaluation point is exactly on a UTC
minute boundary `t`. For each symbol and each fixed horizon `w ∈ {1, 5, 15}`,
it requires every one-minute candle from open `t - (w + 1) minutes` through
open `t - 1 minute` for both sources. A candle opened at `u - 1 minute`, with
`close_time_ms = u - 1`, is eligible at boundary `u` when its source
availability is no later than `u`. At each evaluation boundary `t`, every
candle in the window is checked with `close_time_ms < t` and
`first_seen_at_ms <= t`; an older candle that arrived late becomes usable once
it is visible at `t`. This uses the exchange-close-time-plus-one
millisecond archive surrogate. It is not an observed network receipt time, and
the movement replay's two-second finalization grace is not applied here.
Gaps, duplicate rows, off-grid timestamps, and unavailable source candles are
never filled, interpolated, or converted into zero prices or returns.

For source closes `M` (mark) and `T` (trade), the report records the endpoint
closes and times, log returns, endpoint bases, and
`divergence = log(M(t)/T(t)) - log(M(t-w)/T(t-w))`, equivalently
`mark_return - trade_return`. Positive divergence means the mark/trade log
basis widened over the horizon; an unchanged price and zero divergence remain
valid. Calculations use Decimal logarithms at precision 50. Decimal values are
serialized as fixed-point strings with trailing fractional zeros removed.
Candidate points preserve source-specific availability reasons and same-time
V1 market state as context. Partition summaries describe ready coverage and
common-ready observations; V1 is a comparator, not ground truth, and mark price
is a reference price, not an executable price. The report does not create a
directional classifier, trading signal, or predictive claim.

The extension has independent identities: suite
`historical-mark-trade-extension-suite-v1`, report
`historical-mark-trade-extension-report-v1`, algorithm
`mark-trade-log-basis-divergence-v1`, and config
`MARK-TRADE-LOG-BASIS-1M-5M-15M-DECIMAL50-v1`. Its manifest binds the ordered
symbols, requested interval, warm-up, mark source/type/schema/availability,
verified mark packages and checksums, #111 trade dataset and OHLC evidence,
V1 replay and point-stream identities, partition cutoffs, and code revision.
The extension fingerprint and canonical report SHA remain separate from the
core dataset, replay, point stream, and fixed 28-run experiment identities.
Historical mark-price methodology can change across periods; reports retain
dates and package provenance so later #123 candidate periods can be examined
separately. This point-level diagnostic does not implement the forward-outcome
evaluation deferred to #123.

The CLI reads local archives by default. Mark archive acquisition is opt-in
only with `--download-mark-archives`:

```bash
python -m market_analysis.historical_mark_trade_extension \
  --archive-root /path/to/binance-public-data \
  --mark-archive-root /path/to/binance-mark-price \
  --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT \
  --universe-id research-pilot \
  --universe-version v1 \
  --start 2026-09-01T00:00:00Z \
  --end 2026-09-01T12:00:00Z \
  --development-end 2026-09-01T06:00:00Z \
  --validation-end 2026-09-01T09:00:00Z \
  --output-json mark-trade-extension.json
```

Add `--download-mark-archives` only when an explicit archive acquisition is
intended. No historical coverage or diagnostic usefulness is implied until a
real-archive pilot is run and its coverage and values are reviewed.

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
  --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT \
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

## EXP-75-11 independent derivatives context

Issue #128 supplies three separate descriptive sidecars. The ordered configured
universe in the canonical replay manifest is authoritative; the provisional
five-symbol research universe is not enforced. No source enters the core
`BinanceHistoricalReplayDataset`, movement, classification, lifecycle, fixed
28-run suite, replay fingerprint, point stream, batch manifest or experiment
hashes. These diagnostics add no signals, rankings or #123 forward outcomes.

### Evidence and availability

| Extension | Source / schema | Calculation and availability |
| --- | --- | --- |
| `EXP-75-11-OI` | Binance USD-M daily `metrics`, named `create_time`, `symbol`, `sum_open_interest`; optional `sum_open_interest_value` | `ln(current base quantity / exact prior base quantity)` for 5m/15m. Every usable nominal 5m row becomes eligible at `create_time + 5m`. At least 20m prehistory is loaded. |
| `EXP-75-11-FUNDING` | Binance USD-M monthly `fundingRate`, named `calc_time`, `funding_interval_hours`, `last_funding_rate` | Most recent settled rate / recorded positive interval hours. Eligible at `calc_time + 1ms`; the settlement at exactly `t` cannot affect `t`. Load the preceding calendar month and all study months. |
| `EXP-75-11-LIQUIDATION` | Tardis grouped `binance-futures/liquidations/YYYY/MM/DD/PERPETUALS.csv.gz`; exactly `exchange,symbol,timestamp,local_timestamp,id,side,price,amount` | Observed `price * provider-normalized amount` sums for exchange-event windows `[t-w,t)`, 1m/5m/15m, gated by exact `local_timestamp_us <= t_ms * 1000`. |

OI and funding availability conventions are versioned replay surrogates, not
measured publication latency or network receipt. They do not use movement replay
finalization grace. OI schema normalization is limited to BOM removal, header
whitespace and case. Known additional ratio fields are accepted but unused;
alternative aliases and incompatible headers produce `SCHEMA_MISMATCH`. Funding
`calc_time` must be integer epoch milliseconds. Binance `.CHECKSUM` verification
is mandatory before parsing: byte integrity does not establish completeness.

OI retains base quantity, optional quote value, source/availability timestamps
and package identity. Quantities must be finite and positive. Off-grid rows,
malformed rows, duplicates and missing 5m ranges are reported independently;
all rows at a duplicate timestamp are unavailable. The current row is the latest
eligible row, but a missing latest nominal slot makes its projection unavailable:
older values are retained only as diagnostic metadata. Each 5m/15m endpoint is
exact; no nearest endpoint, interpolation or gap filling. Quote OI is secondary
because price also affects it. Positive OI change does not imply new longs or shorts.

For Binance USD-M metrics archives, accept rows from the package UTC day and an
aligned row exactly at the following day's 00:00 boundary. This accommodates
the observed archive layout before 2026-06-25 while keeping the newer layout's
00:00 row. At the 2026-06-25 transition, if adjacent packages contain distinct
rows at that same source timestamp, global duplicate handling marks the timestamp
ambiguous and unavailable. The availability surrogate remains `create_time + 5m`.

Funding rates may be signed or zero; settlement intervals are not assumed to be
8h. The recorded interval determines `expected_next_calc_time`. At or after
`expected_next_calc_time + 1ms`, absence of a newer usable event produces
`EXPECTED_SETTLEMENT_MISSING`, with the old event retained as metadata and
`funding_per_hour = null`. Source failures remain visible in package diagnostics
and `source_reason`. This expiry is a replay assumption, bound to the identity.
No annualization, normalization or activity thresholds are applied.

Liquidation `sell` snapshots represent long liquidations and `buy` represents
short liquidations. The normalized amount is the provider's CSV quantity; it is
not an eternal equivalence to a particular raw Binance field. Equal exchange
timestamps and repeated supplied rows are preserved in archive capture order.
Rows outside the configured universe are excluded from calculations, but the
whole compressed file SHA binds them too. Products and pooled sums are exact
Decimal operations; ratios/logarithms use precision 50. Decimal diagnostic
outputs use fixed-point strings with trailing fractional zeros removed.

Tardis files partition by **local receipt day**. The compressed-byte SHA is our
own provenance digest, not a provider checksum. Successful full gzip
verification and the compatible schema are required. Empty/header-only,
truncated, unreadable or malformed configured-symbol packages are unavailable;
a malformed configured row conservatively invalidates that entire day. A valid
grouped file containing only other symbols still supplies archive-day coverage.
Disconnect visibility is unavailable in normalized CSV. An available full window
with no rows is `NO_OBSERVED_LIQUIDATION`: long/short/total are zero and imbalance
is null, with no epsilon. This means no observed snapshots in that archive,
not proof of exhaustive exchange liquidation volume or continuous collector health.

Entire window coverage is required. If only a 00:00 UTC free day exists, 1m/5m/15m
windows first become covered at 00:01/00:05/00:15 respectively. Missing preceding
days produce unavailable windows and null numeric results. Receipt microseconds
are never rounded to milliseconds for eligibility. A late receipt cannot change
an earlier output. Event-time indices narrow queries to their windows before
applying receipt eligibility; archives are not rescanned at every replay point.

### Library contracts, summaries and identities

Each `historical_{open_interest,funding,liquidation}_extension` module exposes
its own `Historical…ExtensionRequest`, `Historical…ExtensionPrepared`,
`prepare_historical_…_extension`, `run_historical_…_extension` (prepared input),
`run_historical_…_extension_from_archive`, and canonical report serializer.
Prepared runners allow #123 to reuse one core load/replay. They validate core
ID/version/content SHA, ordered universe, requested range, exact exported movement
evaluation objects and partition labels/cutoffs. There is no combined orchestrator.

Each candidate includes same-time matching V1 direction (funding uses primary
5m). Market summaries expose universe, ready/covered, unavailable, partial coverage
and denominator counts. OI/funding report positive/negative/zero counts and a ready
median. Liquidations expose active/no-observation counts, active imbalance sign
counts, active/covered breadth, exact pooled observed notionals, largest active
symbol and its pooled share (configured order breaks ties). Unavailable sources
never become numeric zero. Partition summaries include per-symbol coverage,
unavailable reasons, and unique OI endpoint-pair or funding settlement counts;
repeated five-second projections are not independent source observations.
Liquidation snapshot projection counts explicitly count projections, not unique
snapshots across overlapping windows.

The three independent manifests bind experiment/suite/report/algorithm/config
versions, source/schema/evidence versions, availability and coverage rules,
ordered universe, study and prehistory ranges, package paths and compressed SHA,
Binance checksum status, evidence SHA, calculation parameters, canonical dataset
identity, replay fingerprint, experiment-stream SHA, partition cutoffs, movement
algorithm/config identities, code revision and complete candidate output SHA.
Each has an extension fingerprint and canonical report SHA. Code revision affects
the extension fingerprint/report, following existing extension conventions.
Moving otherwise identical archives between roots does not affect identity.

### Opt-in acquisition and manual real-source validation

Defaults inspect local sidecars only. OI/funding acquisition uses existing Binance
verified checksum and atomic-download mechanics, without changing core downloader
planning. Liquidation acquisition can fetch only unauthenticated first-of-month
Tardis samples. Existing local files on other days are accepted; absent non-free
days are `FREE_SAMPLE_UNAVAILABLE`, with no authenticated/paid request or API key.
Keep all archives and reports outside the repository.

No real-source validation was performed for this implementation. The following
commands are a **manual post-implementation** check of a small recent free sample;
the core downloader still needs its existing V1 historical lookback. Use the
project's configured Python environment. From the repository root:

```bash
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
mkdir -p /tmp/crypto-watcher-128/reports
common=(--symbols BTCUSDT ETHUSDT --universe-id issue-128-manual --universe-version v1 --start 2026-06-01T00:15:00Z --end 2026-06-01T00:30:00Z)
partitions=(--development-end 2026-06-01T00:20:00Z --validation-end 2026-06-01T00:25:00Z)
python3 -m market_analysis.binance_historical_download --archive-root /tmp/crypto-watcher-128/core "${common[@]}"
python3 -m market_analysis.historical_open_interest_extension --archive-root /tmp/crypto-watcher-128/core --oi-archive-root /tmp/crypto-watcher-128/oi "${common[@]}" "${partitions[@]}" --download-oi-archives --output-json /tmp/crypto-watcher-128/reports/oi.json
python3 -m market_analysis.historical_funding_extension --archive-root /tmp/crypto-watcher-128/core --funding-archive-root /tmp/crypto-watcher-128/funding "${common[@]}" "${partitions[@]}" --download-funding-archives --output-json /tmp/crypto-watcher-128/reports/funding.json
python3 -m market_analysis.historical_liquidation_extension --archive-root /tmp/crypto-watcher-128/core --liquidation-archive-root /tmp/crypto-watcher-128/liquidation "${common[@]}" "${partitions[@]}" --download-free-liquidation-samples --output-json /tmp/crypto-watcher-128/reports/liquidation.json
```

Review package schema/integrity statuses and ready coverage in all three reports.
The 00:15 start avoids requiring the previous non-free liquidation day. OI may
load the previous day for warm-up; funding loads May and June. This June study
date also exercises the pre-change OI archive layout and the free first-of-month
Tardis sample. If a real header differs from the frozen contract, the package
fails explicitly rather than guessing. Reports are descriptive evidence and
have no universal coverage pass threshold. Real-source validation remains
required before closing #128.

## Frozen market-state study Part B

The Part-B driver consumes the checked-in selection manifest at
`research/historical-market-state-study-v1/selection/historical-market-state-study-v1-manifest.json`.
It validates the manifest SHA and does not rerun selection. A study period uses
one verified core dataset load and one canonical replay; the resulting uniform
period-phase points and precomputed V1 classifier/lifecycle branch are shared
with the fixed `EXPERIMENT_SUITE_V1` candidate runners, ATR 06B, and supplementary
extensions. The original 28 experiment identities remain unchanged.

First freeze supplementary source coverage. The default operation reads local
archives only and records verified package/checksum identities, native source
statuses, and per-period/per-symbol coverage. It does not run candidates or
forward labels. Network acquisition is available only through explicit
`--download-*` flags on `coverage`. Keep coverage and execution reports outside
the repository; use the same code revision and archive roots for both steps.

From the repository root, set `ARCHIVE_ROOT` to the existing verified Binance
USD-M archive root and `OUTPUT_ROOT` to a private output directory, then run:

```bash
export PYTHONPATH="$PWD/python${PYTHONPATH:+:$PYTHONPATH}"
ARCHIVE_ROOT="/path/to/verified-binance-usdm-archives"
OUTPUT_ROOT="/path/to/private-historical-market-state-output"
mkdir -p "$OUTPUT_ROOT"
python3 -m market_analysis.historical_market_state_study_execution coverage \
  --study-manifest research/historical-market-state-study-v1/selection/historical-market-state-study-v1-manifest.json \
  --archive-root "$ARCHIVE_ROOT" \
  --output-json "$OUTPUT_ROOT/historical-market-state-study-v1-extension-coverage.json"
```

If mark-price, open-interest, funding, or liquidation archives live under
separate roots, pass their corresponding `--*-archive-root` options to coverage
and execution. A missing or invalid source remains explicit in the frozen
coverage artifact; it does not replace a study date or become zero activity.
Execution refuses coverage from another manifest, source identity, or code
revision. Period artifacts are written atomically beneath `periods/`; identical
finalized periods are verified and skipped on resume, while conflicts are
rejected.

After the coverage artifact is frozen, the first-three-development engineering
smoke command can collect operational timing data in a separate JSONL sidecar.
The sidecar does not enter the scientific period artifacts or their hashes.
For split-root coverage, keep `CORE_ROOT`, `BINANCE_SUPP_ROOT`,
`LIQUIDATION_ROOT`, and `CODE_REV` set to the same roots and code revision used
when coverage was frozen. Use a fresh smoke output directory, runtime report,
and resource report path to record three executed periods instead of resume
skips:

```bash
SMOKE_OUTPUT_DIR="$OUTPUT_ROOT/runtime-smoke"
RUNTIME_REPORT="$OUTPUT_ROOT/runtime-smoke.jsonl"
RESOURCE_REPORT="$OUTPUT_ROOT/historical-market-state-study-v1-smoke-resource.txt"
/usr/bin/time -v -o "$RESOURCE_REPORT" \
python3 -m market_analysis.historical_market_state_study_execution execute \
  --study-manifest research/historical-market-state-study-v1/selection/historical-market-state-study-v1-manifest.json \
  --coverage-manifest "$OUTPUT_ROOT/historical-market-state-study-v1-extension-coverage.json" \
  --archive-root "$CORE_ROOT" \
  --mark-archive-root "$BINANCE_SUPP_ROOT" \
  --open-interest-archive-root "$BINANCE_SUPP_ROOT" \
  --funding-archive-root "$BINANCE_SUPP_ROOT" \
  --liquidation-archive-root "$LIQUIDATION_ROOT" \
  --output-dir "$SMOKE_OUTPUT_DIR" \
  --phase development \
  --period-limit 3 \
  --code-revision "$CODE_REV" \
  --runtime-report "$RUNTIME_REPORT"
```

That limit selects the first three frozen development dates and does not create
a new sample. The period-report v2 contract also persists two Part-C
prerequisites without recomputation: a deduplicated exact-boundary V1 context
for every causal event and a separately hash-bound BOCPD causal-onset
observation section. BOCPD detection-region end/observed-through metadata stays
descriptive and is not the causal onset predictor.

Run the development phase without `--period-limit` to complete all ten periods.
Before validation, build the deterministic leave-one-development-day-out HMM
cross-fit artifacts from those ten finalized period reports:

```bash
python3 -m market_analysis.historical_market_state_hmm_crossfit \
  --study-manifest research/historical-market-state-study-v1/selection/historical-market-state-study-v1-manifest.json \
  --output-dir "$OUTPUT_ROOT/full-study" \
  --code-revision "$CODE_REV"
```

The cross-fit tool is artifact-only: it accepts no archive roots or download
flags. Each of the ten folds trains the existing HMM on the other nine stored
development training blocks and causally filters the held-out stored feature
blocks, resetting at stored gaps. It writes ten immutable fold artifacts plus a
hash-linked index. The held-out day never enters its fold model.

Then run `freeze-hmm` on the complete ten-day development cohort for the final
all-development HMM used by validation/test. Validation requires that frozen
development-only HMM artifact, and test additionally requires an explicit
`--allow-test` acknowledgement. Part B stores descriptive native candidate
evidence and candidate-independent trade-price outcomes only. It does not
perform Part-C nomination, validation decisions, regression, bootstrap,
multiple-testing adjustment, scoring, promotion, or evidence classification.

Forward prices use the latest completed trade-price one-minute candle with
`close_time_ms < t` and `first_seen_at_ms <= t`; event times remain on their
actual five-second boundary. The endpoint must be exactly `h` minute candles
after the start anchor or that symbol's return is unavailable. The frozen
unannualized return is `ln(P(t+h) / P(t))`; market return is its median across
available symbols, positive breadth is `count(r > 0) / available_symbol_count`,
realized volatility is `sqrt(sum(q²))` over the complete adjacent one-minute
path without demeaning or annualizing, and cross-sectional dispersion is the
unscaled median absolute deviation around the median return. Missing endpoints
and missing realized-volatility path minutes remain explicit unavailable
outcomes. Future label-tail candles are loaded separately and never enter
candidate replay or V1 state.

Continuous outcomes use the non-overlapping UTC grids (1,896 decision/horizon
keys per complete day). CUSUM, BOCPD, HMM, and V1 events retain their exact
causal onset; later events remain in the report and are marked by
horizon-specific confirmatory independence. PELT evidence is retrospective and
never receives forward labels. Secondary V1 state paths are censored whenever
their terminal point is outside the selected day's replay interval; an
evaluable path with no reversal records `CENSORED_NO_REVERSAL`, while gaps in
the V1 path make the reversal label unavailable.
