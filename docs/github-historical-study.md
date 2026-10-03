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
  --output /owner/bundles/development-0-v1
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
The elapsed budget uses a parent monotonic clock; budgets never enter scientific
configs, input descriptors, algorithm identities or scientific hashes.

Between-stage budgets do not bound indexing, one large calculation, spool creation,
scientific hashing or final serialization. The separate supervisor requests parent
cancellation before the 15-minute reserve, reaps the scientific child, and kills
its process group if required. Recovery acquires campaign/period ownership and
refuses to snapshot an active/orphan worker. Only previously committed valid units
survive an interrupted calculation. A hard hosted-job kill is not recovery.
Partial replay currently rebuilds the disposable SQLite trade index on resume.
A unit that repeatedly cannot finish within the deadline needs a longer explicitly
budgeted job, genuine unit resume work, or a VM; changing study dates/algorithms is
not a workaround.

## First dispatch and bounded manual continuation

No automatic continuation, matrix or schedule exists. Global workflow concurrency
serializes the initial platform with `cancel-in-progress:false`. Do not cancel a
working job casually. The workflow validates each restored unit in staging using
the existing replay/spool/stage/report validators. Validation resolves references
against the staging mirror without rewriting their stored JSON, then installs the
verified files at stable paths. Input preflight checks original file hashes and
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
fit. The default single dispatch is 60 minutes. Allowed allocations are 16–350,
with a 15-minute reserve; configured job timeout is 355, below the hosted six-hour
limit. Time is counted from the owned job API's `started_at` before checkout/acquisition,
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
contains only an allowlisted receipt: state, sizes, allocation, exact tag and sealed
manifest SHA. Raw inputs, detailed evidence, spools, failed job/request diagnostics
and logs stay in the private data repository. Publication failure fails the workflow;
a successful saved recovery does not change a `FAILED` scientific status to success.

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
