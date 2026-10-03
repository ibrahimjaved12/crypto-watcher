# Manual GitHub execution of the frozen study

This implements the initial platform for #163. The hosted pilot is **not yet run**.
Source review cannot establish scientific parity, full-day memory requirements,
longest-stage fit, or three-period acceptance (#152). No visibility changes, input
uploads, Release creation, secret creation or workflow dispatch are performed by
this implementation PR. `verify.yml` remains unchanged.

The manual workflow must first be merged to **main**. It runs only from main,
checks out the trusted campaign's full SHA after proving it is reachable from
main, and verifies clean Python package source at that actual HEAD. Initially the
orchestration pin and runtime pin must be the same trusted implementation commit;
both are recorded independently. The frozen coverage's producer revision remains
the scientific producer. Never substitute the new runtime SHA for that producer.

## Owner setup

1. Create/configure a **private companion repository** for restricted research
   data. Set `RESEARCH_DATA_REPOSITORY=owner/private-data-repo` as a repository
   variable in the code repository. The workflow refuses public or same-repository
   data storage through the GitHub API before download or upload.
2. Create `RESEARCH_DATA_TOKEN`, a fine-grained Contents read/write token restricted
   to that companion repository, as a code-repository Actions secret. Transport
   steps alone receive it. Scientific subprocesses receive a small allowlisted
   environment with no data or code token. Code checkout uses `contents:read`; `actions:read` reads only the owned job start timestamp, and
   does not persist its credential. There is no public Actions artifact upload.
3. Confirm permitted provider access and redistribution terms for supplied inputs.
   Private storage is not permission to redistribute. Supply existing original
   archives/coverage; no Tardis credential probing or automatic provider download
   occurs. If exact frozen bytes cannot be reproduced, obtain the original bundle.
4. Prepare selected-period input bundles offline using the command below. Do not
   refreeze coverage or improve an unavailable source because data appeared later.
   Missing, originally invalid or originally absent packages keep their original
   facts. Coverage loaders that failed without recording package identities bind the
   supplied planned original bytes and exact failure summary in the trusted
   inventory; preflight must reproduce that summary. Later downloads for known
   frozen absences are excluded without deleting owner files.
5. Publish each bundle as a versioned private input Release; record its sealed
   inventory SHA from the tool and its explicit tag in the campaign JSON. Do not
   edit these input Releases after pinning. Upload all `part-*` files and
   `manifest.json`. No public code Release should contain these restricted bytes.
6. Copy `research/campaigns/github-pilot.example.json` to a real campaign spec,
   fill the missing owner values, and commit the spec to main normally. The example
   intentionally contains null pins and will fail validation. Use a safe campaign
   ID of at most 64 alphanumeric/underscore/hyphen characters. Never reuse an ID to
   conceal previously allocated runs. Pin the already merged implementation SHA,
   both dependency-lock SHA-256 values, original study/coverage file SHA-256 values,
   their canonical identity hashes, and the coverage producer's full Git SHA.

Do not launch a workflow from a PR, fork or arbitrary ref with the data token. The
campaign supplies configuration, never shell commands or repositories to execute.
GitHub storage permissions must allow the token to enumerate private drafts and
upload/publish private Releases. The workflow creates allocation reservations and
recovery Releases only when the owner manually dispatches it later.

## Offline bundle preparation (future owner command)

From the owner's existing checkout with the locked Python dependencies available:

```sh
PYTHONPATH=python python3 -m market_analysis.historical_study_bundles prepare-input-bundle \
  --study-manifest /owner/frozen/study-manifest.json \
  --coverage-manifest /owner/frozen/extension-coverage.json \
  --phase development --period-index 0 \
  --core-root /owner/archives/core --mark-trade-root /owner/archives/mark \
  --open-interest-root /owner/archives/open-interest \
  --funding-root /owner/archives/funding --liquidation-root /owner/archives/liquidation \
  --output /owner/bundles/development-0-v2
```

This inventories/packs existing files; it never runs replay, generates a new frozen
manifest, or downloads all 30 periods. The inventory binds exact period membership,
source roots and canonical names, per-source package/absence facts, asset order,
file sizes, original/frozen SHA values and every chunk SHA. Core uses the existing
required-date planners, checksums and frozen label-tail identity. Mark uses its
16-minute warm-up and last minute cutoff; OI uses the shared 20-minute/inclusive
end-day planner; funding uses the previous calendar month through the end-boundary
month; liquidation uses receipt days from start minus 15 minutes through end day.
The extracted pure OI/funding/liquidation planners are also used by their loaders.

Bundles consist of **raw ordered file chunks**, each at most 1 GiB. Already
compressed ZIP/gzip inputs are not recompressed. Transfer and assembled byte counts
are measured independently (equal for this format). Files larger than 1 GiB are
split with both whole-file and chunk hashes. This deliberately avoids tar/ZIP
container extraction metadata; duplicate/absolute/traversal names, symbolic links,
hardlinks and undeclared files cannot be installed. Source files must be regular
files through real directory paths; pass resolved owner paths rather than links.
Shared warm-up packages are deduplicated within each selected-period inventory.
Subsequent period inventories may repeat shared files; they do not require the
whole all-period archive. A partition that does not fit disk limits must be
replanned/provisioned explicitly, never silently omitted.

For later phases add existing prerequisite artifacts using `--prerequisite NAME=PATH`.
Names install under `manifests/prerequisites/NAME.json`:

- `development`, `hmm-model`, and `crossfit-index` for validation/test;
- each HMM fold under `hmm-development-crossfit/<fold filename without .json>`,
  preserving the actual directory and filenames referenced by the cross-fit index;
- `validation` and `authorization` additionally for test.

These must be existing artifacts, not improvised JSON. The portable gate uses
`read_artifact`, `load_hmm_prerequisites`, `require_prerequisites` and
`core.authorize_test`. It validates complete development/HMM parents and the exact
validation/authorization chain before test archives or reports are consumed. Test
also requires the explicit `allow_test=true` dispatch flag. The offline inventory
can package test bytes without executing or authorizing test science; dispatch
still enforces the frozen gates.

## Stable runner layout and statuses

Every fresh runner uses exactly:

```text
/tmp/crypto-study/repo
/tmp/crypto-study/inputs/{core,mark,open-interest,funding,liquidation}
/tmp/crypto-study/manifests
/tmp/crypto-study/campaigns/<campaign-id>/{outputs,checkpoints,operations}
/tmp/crypto-study/transfers
```

Stored operational requests contain absolute paths. Recovery restores these exact
paths, never patches requests or relabels old laptop/pre-#168 checkpoints. Recovery
must match runtime, orchestration, dependency locks, original study/coverage/input
identities, campaign spec, membership and layout exactly. Known legacy **source
coverage** can retain its original bytes/hash; derived taker-flow evidence stays
`v2-exact-sign`. Changing the campaign spec intentionally invalidates recovery pins;
use a deliberately planned new campaign for changed operational bounds.

`operations/status.json` is atomic and separate from scientific evidence:

- `YIELDED`: preflight-only or a safe operational budget boundary; no finalized
  report, complete-period runtime record or complete aggregation is fabricated.
- `FINALIZED_PERIOD`: the exact frozen report is durably finalized/revalidated.
  Aggregation additionally records its exact expected set and `complete_phase`;
  a three-period subset is not a complete ten-period development phase.
- `FAILED`: scientific/validation/watchdog failure remains failed even if prior
  committed work was saved remotely. Read the private per-run logs for diagnostics.

Replay yields after atomic hourly checkpoints. At the final checkpoint, normal
budget yielding is deferred until COMPLETE spool and prepared replay publication.
New stage limits count **verified durable results**, including unavailable-source
stages. Reused results do not consume the new-stage budget. Stage lookup/reuse
precedes the new-work check, permitting final assembly on a later fresh slice.
The hosted elapsed budget maps the recorded job start to one absolute compute deadline, converted to a monotonic deadline before restoration/preflight; budgets never enter scientific
configs, input descriptors, algorithm identities or scientific hashes.

Between-stage budgets do not bound indexing, one large calculation, spool creation,
scientific hashing or final serialization. The separate supervisor requests parent
cancellation before the 15-minute reserve, reaps the scientific child, and kills
its process group if required. Recovery acquires campaign/period ownership and
refuses to snapshot an active/orphan worker. Only previously committed valid units
survive an interrupted calculation. A hard hosted-job kill is not recovery.
Partial replay rebuilds the disposable SQLite trade index by default. An explicitly enabled, exact prepared-core cache can avoid archive decoding and index construction; raw-source verification still runs.
A unit that repeatedly cannot finish within the deadline needs a longer explicitly
budgeted job, genuine unit resume work, or a VM; changing study dates/algorithms is
not a workaround.

## First dispatch and bounded manual continuation

No automatic continuation, matrix or schedule exists. Global workflow concurrency
serializes the initial platform with `cancel-in-progress:false`. Do not cancel a
working job casually. The workflow validates each restored unit in staging using
the existing replay/spool/stage/report validators. Validation resolves references
against the staging mirror without rewriting their stored JSON, then installs the
verified files at stable paths. Both transport and portable restore classify only
exact frozen checkpoint directory and report/sidecar names in the sealed inventory,
and reject periods outside the campaign's declared membership before scientific
recovery is read. Campaign IDs such as `study-test-pilot` and operation log names
are opaque: their spelling never supplies a phase. Development refuses genuine
validation/test evidence; validation refuses genuine test evidence. Earlier-phase
work remains usable by appropriately authorized later phases, and explicit test
authorization plus the exact validated parents are required before test evidence
is opened. Input preflight checks original file hashes and
selected source coverage; it does not parse aggTrades twice.

After owner setup and the workflow/campaign are on main, use these exact dispatch
values. Replace the spec path and copy locator/hash from each verified job summary.
These are future owner commands, **not executed in this implementation**:

```sh
gh workflow run historical-study.yml --ref main \
  -f operation=preflight -f campaign_spec=research/campaigns/pilot.json \
  -f phase=development -f period_index=0 -f allow_test=false -f job_minutes=20

# Copy the preflight receipt's exact private generation and sealed manifest SHA.
gh workflow run historical-study.yml --ref main \
  -f operation=execute-period -f campaign_spec=research/campaigns/pilot.json \
  -f phase=development -f period_index=0 -f allow_test=false -f job_minutes=50 \
  -f resume_generation=RECOVERY_TAG_FROM_PREFLIGHT \
  -f resume_manifest_sha=SEALED_MANIFEST_SHA_FROM_PREFLIGHT

# Fresh runner, same exact runtime/spec/period; use the second receipt, not latest.
gh workflow run historical-study.yml --ref main \
  -f operation=execute-period -f campaign_spec=research/campaigns/pilot.json \
  -f phase=development -f period_index=0 -f allow_test=false -f job_minutes=50 \
  -f resume_generation=RECOVERY_TAG_FROM_SECOND_RUN \
  -f resume_manifest_sha=SEALED_MANIFEST_SHA_FROM_SECOND_RUN
```

That plan conservatively reserves **20 + 50 + 50 = 120 runner-minutes total**, not
120 per slice. It is an upper allocation, not a prediction that a full period will
fit. The default single dispatch is 60 minutes. Workflow choices are exactly
16, 20, 30, 50, 60, 120, 180, 240, 300 and 350 minutes, with a 15-minute reserve.
The main-branch job guard rejects values outside that choice set, and the selected
allocation also sets GitHub's whole-job hard timeout, including checkout/setup.
Python retains its 16–350 validation and earlier cancellation deadline so normal
snapshot/publication finish inside the allocation with their final grace; the hard
timeout is a last safeguard. Time is counted from the owned job API's `started_at` before checkout/acquisition,
including runner setup already elapsed before the first user step. Dependency
installation, network operations, verification/packing and uploads all have bounded
timeouts against that deadline. Snapshot and upload leave additional final grace.
If the runtime cannot establish the start or verify the pins it fails before science.

The private repository maintains immutable per-run allocation tags, including
interrupted draft reservations. Omitting the resume locator does not reset the
ceiling. The sealed recovery lineage also binds run count, consecutive no-progress
count and sampled cumulative runner minutes. Two consecutive no-progress slices
are the example retry cap; the next retry is refused. Full job allocations remain
reserved even for early failures. Measured minutes are labeled by their sampling
boundary, not claimed as exact process-exit totals. Changing an exhausted campaign
requires an explicit owner decision and a new/reviewed campaign, not deleting the
ledger or retrying blindly.

A private recovery generation is unique to campaign/run/attempt. Parts upload first,
sealed manifest last, then API size/digests (or streaming readback hashes) verify
before the private draft is published. Partial drafts and failed uploads are not
resumable completion. Do not delete older good generations. The small public summary
contains allowlisted operational observations and the exact verified continuation receipt. Computed replay progress is separate from committed checkpoint/stage progress. Raw inputs, detailed evidence, spools, failed job/request diagnostics
and logs stay in the private data repository. Publication failure fails the workflow;
a successful saved recovery does not change a `FAILED` scientific status to success.

Snapshot retains the exclusive campaign lease without broad campaign cleanup.
After the supervisor terminates/reaps science, each exclusive period lease runs
the same known temporary cleanup as execution before validation and packing. It
unlinks only established atomic `.<filename>.<random>.tmp`, `.shared-v1-*` and
`.*.records-*` temporary names in the period root, post-replay, chunks and states,
and handles known abandoned `.study-points-*` directories without following
symlinks. This removes a temporary alias left between publication's hardlink and
unlink while preserving the published bytes and hashes. Published units and
request/job diagnostics remain intact; arbitrary remaining hardlinks, symlinks
and non-regular files still fail strict validation. Active/orphan ownership still
blocks snapshot acquisition. Forced-kill recovery has not been executed or verified.

## Aggregation and scientific sequence

The campaign declares exact expected membership per phase. To aggregate the declared
development subset after every required report exists, dispatch:

```sh
gh workflow run historical-study.yml --ref main \
  -f operation=aggregate -f campaign_spec=research/campaigns/pilot.json \
  -f phase=development -f job_minutes=20 \
  -f resume_generation=EXACT_RECOVERY_WITH_ALL_EXPECTED_REPORTS \
  -f resume_manifest_sha=SEALED_MANIFEST_SHA
```

Aggregation needs its own declared allocation; the preceding 120-minute pilot leaves
none and does not promise aggregation or three-period acceptance. A deliberate later
campaign/allocation is required. Every report is validated using the existing
finalized loader, exact filename/period, producer/coverage, report/section hashes and
duplicate/conflict rules before `_update_execution_index` is used. Partial native
indexes remain operational partial indexes. Missing/unexpected reports fail exact
membership validation; never concatenate JSON or delete conflicts to force success.

The scientific sequence remains manual and owned by existing modules:

1. Development Part-B reports for all ten frozen development periods.
2. `historical_market_state_hmm_crossfit` plus Part-B `freeze-hmm`.
3. `historical_market_state_study_part_c development` freeze.
4. Validation Part-B with frozen HMM/development prerequisites.
5. Part-C validation and its exact aggregate test authorization.
6. Untouched test Part-B with `--allow-test` and the validated chain.
7. Part-C test.

Those modules' existing CLIs own the science. The platform neither automatically
advances phases nor refactors or bypasses their gates. Exact period indexes are
0–9 development, 10–17 validation and 18–29 test, with an explicit matching phase;
`--period-index` cannot combine with `--period-limit`.

## Failure, footprint and pilot review

Disk observations sample raw input, transfer/staging, checkpoints and output bytes,
free disk, and sampled high-water usage. Existing #168 stage durations/RSS stay
outside scientific hashes; samples are not invented process-exit peaks. Input and
recovery transfer/uncompressed sizes are recorded from actual bytes. The example
limits are stop conditions, **not measurements**: 5 GiB transfer/recovery limits and
6 GiB working/upload headroom. Hosted disk may be insufficient once raw packages,
disposable SQLite, replay chunks, spool, shared V1, records and the upload copy
coexist. Measure it; provision more disk or change the operational plan explicitly.
There is no under-two-hour or constant-memory promise.

On hash/runtime/source/authorization mismatch, stop and inspect private identities.
Never regenerate coverage, overwrite conflicting requests, wipe input facts, or
restore “latest.” A failed watchdog unit can be retried only from a verified
explicit generation within its caps. If snapshot/upload fails, use the preceding
verified generation and account for the failed allocation. Owner cleanup may remove
abandoned local temp files after exclusive acquisition or inspect private drafts;
do not delete good recovery/allocation history. No implementation command changes
repository visibility or paid resources. Returning a public repository to private
does not revoke prior public copies and brings private compute quotas back.

Within the 120-minute pilot, first establish preflight → one durable unit → fresh
runner resume. Use a deliberately incorrect trusted restore hash to demonstrate
negative validation before science (it still consumes a conservative allocation);
budget that instead of a progress run if necessary. Small uninterrupted/resumed
parity is a later bounded allocation if feasible. A short diagnostic must never be
presented as a completed frozen period or a full memory/parity validation. Record
footprint, checkpoint/reuse hashes, longest measured unit, private publication and
failure behavior before choosing the next campaign. Full three-period acceptance
remains #152.

Primary platform contracts: [manual dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch),
[hosted limits](https://docs.github.com/en/actions/reference/limits), and
[Release asset API/digests and authenticated redirects](https://docs.github.com/en/rest/releases/assets).


## Operational events and deadline outcomes (new pinned runtime)

The private `operations/*-RUN-ATTEMPT.events.jsonl` channel uses
`historical-study-operational-event-v1`: UTC timestamp, monotonic elapsed time,
operation, phase, exact period, stage/category and remaining compute seconds.
Records are limited to 4 KiB, with a 16 MiB ceiling per channel. Transport emits
request/asset completion, actual downloaded bytes (including retries), selected
file bytes and unique required asset bytes (including packing overfetch).
Download, hash, reconstruction, installation, recovery validation, core
preparation, replay and stage timing observations are incremental. An interrupted
span records elapsed time without claiming completion. Nested timing spans are
not additive whole-job percentages.

Only allowlisted identifiers and finite counters are relayed as `study-event`
JSON in Actions stdout. The relay never reads child stdout/stderr, request
payloads or private traceback files. Event strings cannot introduce workflow
commands. Heartbeats occur about every 45 seconds during long preparation/replay
spans and worker waits. They sample parent/worker RSS, cumulative CPU activity and
free disk; stage completion also flattens the existing `_observation` memory
measurements. CPU counters describe each observed process, not summed campaign
CPU. Directory size scans remain at most once per minute in the slice controller,
including its existing `/tmp/binance-core-verify-*` SQLite sampling. Disk/RSS
high-water observations are sampled values, not guaranteed process-exit peaks.

`REPLAY_RESUMED` identifies validated restored boundaries and checkpoint SHA.
`REPLAY_CURRENT_PROGRESS` describes computed boundaries, including work not yet
checkpointed; it does not serialize state. `REPLAY_CHECKPOINT_WRITTEN` identifies
durable hourly work. Stage STARTED, COMPLETED and REUSED are distinct: a heartbeat
in a running stage does not mean that stage is committed. Stage count and replay
percentages describe those units only, never whole-campaign completion.

Hosted compute ends at **actual job start + job_minutes - 15 minutes**. The child
receives that epoch, so dependency setup, downloads, restoration and preflight
cannot restart its budget. It cooperatively checks 30 seconds before the compute
cutoff at existing durable checkpoint/stage boundaries and before new work. Final
replay yielding is still deferred until the COMPLETE spool and prepared replay
are published. Standalone `--max-elapsed-seconds` remains available, starting
before restoration; hosted `--compute-deadline-epoch` takes precedence.

If science cannot unwind cooperatively, the supervisor signals the entire process
group, allows up to 10 seconds to unwind, then kills/reaps it. It acquires campaign
and period ownership, cleans only recognized abandoned runtime temporaries,
validates the complete recovery closure and verifies retention of the exact
parent's committed work. Only a verified budget termination becomes `YIELDED`
with `supervisor-budget-termination`. The last durable unit is derived from the
validated artifacts, not a progress log. Calculation/subprocess failures,
external cancellation and integrity failures stay `FAILED`; successful recovery
publication cannot turn them into successful computation. An unfinished stage
has no fabricated resumable stage state.

The scientific step retains its failure exit code. The publication step succeeds
when its remote bytes/publication are verified, including a `FAILED` scientific
slice; the overall job still fails through the scientific step. A verified budget
yield succeeds without claiming period completion. The final summary runs with
`always()` while job time remains, even after setup, computation, snapshot or
publication failure. It reports scientific/publication states separately,
new/reused stages, computed versus committed progress, phase/stage timings,
transfer/resource samples and the exact receipt when available. A hard platform
kill can still prevent any summary or upload.

## Packed transfer format v2

New input and recovery bundles use `historical-study-file-bundle-v2`. Their sealed
inventory contains a top-level asset table (`asset`, byte size, SHA-256 and
partition) and full-file inventory with exact size/SHA, original source facts,
explicit absence and `extents`: `asset`, `asset_offset`, `file_offset`, `length`.
Packing is deterministic, streams bounded buffers and caps each raw asset at
1 GiB. ZIP/GZ source bytes are not recompressed. Multiple small files share one
asset; a large file may span assets. Metadata assets are separate from raw inputs,
and incompatible period/phase partitions cannot share assets. Unknown or
later-phase prerequisite metadata is rejected before metadata asset acquisition.

Each required asset downloads once and is verified. Files reconstruct atomically
using bounded reads and whole-file hashes. Readers reject unsafe paths, duplicate
membership, overlapping/out-of-range extents, incomplete file/asset coverage,
wrong hashes and configured byte/headroom limits. Selected reconstruction bytes
and unique downloaded asset bytes are measured separately: packing can require
extra asset bytes even when only a subset of files is selected.

Original v1 inventories and recovery remain readable with their original bytes
and hashes. Do not edit manifests, relabel runtime identities or overwrite Release
tags. The workflow detects the exact pinned helper's capabilities; old pilot pins
skip the new dependency-key/cache/summary commands and retain their original
execution behavior. New campaigns must use new exact runtime/orchestration pins.
The existing pilot and saved evidence are not migrated or modified.

Transfer/staging copies are deleted only after verified installation and after
semantic recovery validation has consumed the staging tree. Within one owned,
unchanged tree, in-process typed receipts reuse full replay/spool validation and
source-byte preflight results. Execute slices defer supplementary decoding to its post-replay consumer instead of retaining a second evidence graph through replay; preflight-only still validates semantic source coverage. They bind installed file identities,
including explicit absences, and never persist a cross-job "verified" flag.
Changed trees/new trust boundaries require full validation again.

## Optional private prepared-core cache

Caching is off unless a **new pinned campaign** sets `prepared_cache.enabled`
with positive `max_bytes`, `headroom_bytes` and `upload_max_seconds`. The production
template demonstrates bounds; choose them from measured runner headroom. It uses
separate content-addressed private data Releases, tagged
`prepared-core-<first 48 characters of exact identity SHA-256>`, with the full
identity checked inside the sealed inventory. There is no mutable latest locator.

The identity binds the exact input inventory and raw-package hashes/absences,
frozen study/coverage/period/universe/configuration, entire campaign, runtime and
both dependency locks, numerical correction, cache schema, Python patch version,
SQLite version, OS/architecture and byte order. The local immutable cache contains
a closed committed SQLite index and all typed dataset metadata: archive and
normalized-stream manifests, instruments, source intervals, candles, diagnostics,
OHLC and taker-flow evidence. JSON preserves Decimal strings, tuples and versioned
scientific dataclasses; no pickle or lossy numeric decoding is allowed.

Python validates the cache seal, exact identity, database content hash, schema,
read-only SQLite integrity, bounded row count/order/normalized-stream hash,
duplicate identities and typed scientific metadata relationships. The canonical
trade cursor, including last-source-trade-key resume ordering, is unchanged.
Closing a cache reader closes its connection without deleting shared immutable
files; transient raw preparation retains its existing temporary cleanup. Cache
construction copies only a closed committed database and atomically seals the
complete metadata/database directory after successful raw preparation.

Complete prepared replay and point-spool reuse run first. A completed replay does
not fetch an index. Absent/incompatible/corrupt/oversized optional caches or
inadequate download headroom emit misses and fall back to verified raw loading;
scientific input/checkpoint corruption remains fatal. A rejected immutable cache
is not overwritten in place. Repair requires a new reviewed identity/publication,
not changing saved evidence.

Authenticated cache lookup/download/publication stays in `github_study.py`;
scientific subprocesses receive only local paths and remain token-free. Cache
publication is optional and happens **after verified mandatory recovery**, with
an explicit size/headroom limit and bounded upload window leaving final job grace.
A cache upload failure cannot fail a successful recovery publication. Download,
validation, build and upload costs are reported separately. A cache can cost more
to transfer/validate than it saves: measure before enabling it broadly.

This does not eliminate all raw downloads: original bytes are still verified,
forward labels load their candle tail and supplementary evidence still uses raw
packages. Private datasets/caches/scientific evidence never enter public Actions
cache. Only pip download/wheel files are cached; the key uses OS/architecture,
Python version and hashes of **both actual pinned requirements files after
bootstrap**, not `hashFiles` paths outside `GITHUB_WORKSPACE`. Dependency cache
restore/save is optional; installation remains deadline-bounded.

## Longer jobs and owner-initiated acceptance

`research/campaigns/github-production.template.json` is deliberately rejected
while `template_only` is true and pins/membership/total budgets are empty. It
illustrates a 300-minute allocation, 15-minute publication reserve and
`max_new_stages=64`. To use it, the owner creates a new campaign file/ID, supplies
new main-reachable exact runtime/orchestration/dependency pins, original frozen
manifest/coverage/input locators, authorized scientific membership and explicit
campaign minute/run/no-progress budgets; explicitly authorizes expanded budget;
and removes the template guard. Dispatch `job_minutes=300` explicitly. Do not
edit `research/campaigns/pilot.json` to continue a production run. Longer jobs do
not alter mathematics or authorize validation/test work or expand membership.

After each successful publication, copy **both** `continuation.resume_generation`
and `continuation.resume_manifest_sha` from the summary into the next manual
dispatch's `resume_generation` / `resume_manifest_sha`. Use the same pinned
campaign, phase and exact period for continuation. A locator without its inventory
hash, a draft generation, a different campaign/runtime or unverified publication
is not a resume receipt. Existing allocation/run/no-progress ceilings still apply.
Dispatch remains manual-only, serialized, with `cancel-in-progress: false` and the
actual whole-job timeout. No automated redispatch is added.

The following are owner-initiated hosted follow-ups, **not implementation checks**:

1. Under a newly authorized bounded campaign, verify cooperative and hard budget
   stops, process-group/lease cleanup, parent-work retention, exact continuation,
   last durable checkpoint/stage, and FAILED-versus-YIELDED/publication semantics.
2. On copied inputs under new immutable tags, verify v2 multi-file/split-file
   reconstruction and corruption rejection, v1 restore compatibility, phase
   separation, exact source hashes/absence and unique-byte/overfetch accounting.
   Repack/publish real private input Releases only as a separate owner operation.
3. Compare pinned uninterrupted/resumed and cache-hit/miss scientific outputs,
   including Decimal correction, five-second boundaries, frozen phase/HMM
   dependencies, source absence, warm-up and forward outcomes.
4. Measure cache build/transfer/validation/upload against raw preparation, longest
   stage, disk/RSS and complete setup/compute/publication timings on small hosted
   runs before authorizing a larger campaign. No full-study completion is inferred
   from source review, a saved slice or a small hosted pilot.
