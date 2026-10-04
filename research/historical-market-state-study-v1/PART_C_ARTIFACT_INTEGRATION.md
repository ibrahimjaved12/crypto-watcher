# Part C artifact integration

Part C consumes verified finalized Part B artifacts. It never opens market archives,
executes replay/candidates, computes forward labels, or trains/filters HMMs.
The producer revision is an explicit scientific input, independent of the consumer
Git revision. The existing Part C core owns all fitting, phase decisions and statistics.

From `python/`, with the research requirements installed, use:

```sh
python -m market_analysis.historical_market_state_study_part_c development \
  --study-manifest "$STUDY_MANIFEST" --coverage-manifest "$COVERAGE_MANIFEST" \
  --part-b-output-dir "$PART_B_DEVELOPMENT_DIR" --part-b-revision "$PART_B_REVISION" \
  --hmm-crossfit-index "$HMM_CROSSFIT_INDEX" --final-hmm-model "$FINAL_HMM_MODEL" \
  --output "$DEVELOPMENT_FREEZE"

python -m market_analysis.historical_market_state_study_part_c validation \
  --study-manifest "$STUDY_MANIFEST" --coverage-manifest "$COVERAGE_MANIFEST" \
  --part-b-output-dir "$PART_B_VALIDATION_DIR" --part-b-revision "$PART_B_REVISION" \
  --hmm-crossfit-index "$HMM_CROSSFIT_INDEX" --final-hmm-model "$FINAL_HMM_MODEL" \
  --development-freeze "$DEVELOPMENT_FREEZE" --output "$VALIDATION_FREEZE" \
  --test-authorization-output "$TEST_AUTHORIZATION"

python -m market_analysis.historical_market_state_study_part_c test \
  --study-manifest "$STUDY_MANIFEST" --coverage-manifest "$COVERAGE_MANIFEST" \
  --part-b-output-dir "$PART_B_TEST_DIR" --part-b-revision "$PART_B_REVISION" \
  --development-freeze "$DEVELOPMENT_FREEZE" --validation-freeze "$VALIDATION_FREEZE" \
  --test-authorization "$TEST_AUTHORIZATION" --output "$TEST_REPORT"
```

Each phase opens only its exact frozen period filenames under `periods/` (10/8/12).
Validation verifies the development HMM prerequisite artifacts without inference.
Test validates all parents and reproduces the aggregate authorization before any test
report is opened. Zero authorized members produce a conservative full-family report
without test file access. Test reports commit to the exact opened test provenance.

The fixed implementation registry includes unavailable configs explicitly. Candidate
records cannot introduce configs. V1/candidate/outcome joins use exact persisted
boundaries and primary horizons; unavailable evidence excludes paired rows. Development
HMM rows come only from verified held-out folds. Later rows bind the frozen final model.

Artifacts use a closed typed schema registry, constructor validation, recomputed inner
hashes, and canonical envelope hashes. Atomic publication is create-only. Identical
canonical content resumes safely; changed content or parent identities conflict. Hashes
exclude paths, machine details, timestamps and runtime measurements. Track A preserves
native summaries (including retrospective PELT) separately from predictive results.
Layer 1 reports counts/day counts/mean/median under the frozen bins/categories.

## Required action before the real three-period smoke

The study-specific lossless JSON repair changes the Part B producer revision. After
this PR merges, re-freeze coverage with that merged revision, into a **new** destination.
Keep the prior coverage artifact as an audit/parity reference; do not relabel it.
Run the existing Part B coverage command yourself from `python/`, using the same local
source roots as the prior coverage run:

```sh
PART_B_REVISION=$(git rev-parse HEAD)
python -m market_analysis.historical_market_state_study_execution coverage \
  --study-manifest "$STUDY_MANIFEST" --archive-root "$CORE_ARCHIVE_ROOT" \
  --mark-archive-root "$MARK_ARCHIVE_ROOT" \
  --open-interest-archive-root "$OPEN_INTEREST_ARCHIVE_ROOT" \
  --funding-archive-root "$FUNDING_ARCHIVE_ROOT" \
  --liquidation-archive-root "$LIQUIDATION_ARCHIVE_ROOT" \
  --code-revision "$PART_B_REVISION" --output-json "$NEW_COVERAGE_MANIFEST"
```

Then use that new coverage manifest and the same pinned revision for the representative
three-development-period Part B smoke. Part C development requires all ten finalized
development reports plus the complete cross-fit index and frozen final HMM, not a
three-period subset. No real coverage/study/smoke execution is part of this PR.

## Memory: one period report at a time

Part C and the HMM tools (`freeze_study_hmm_model`, the cross-fit build and
`validate_hmm_crossfit_index`) hold one decoded period report in memory at a
time. Part C loads, validates and hash-verifies each report once, adapts every
config for that period from a single index of its candidate evidence, then
releases it before reading the next. Peak memory is roughly 10-11 GB for one
report's read plus decode, so a 16 GB runner is enough.

Part C validation also needs the ten development reports in the directory that
contains the cross-fit index, because `validate_hmm_crossfit_index` re-validates
them.

No artifact changed: development, validation and test freezes, HMM fold, index
and model artifacts, and all their hashes are byte-identical.

## Analysis tables

A period report is ~2.5 GB on disk and ~8 GB decoded, yet Part C and the HMM tools
read only a small part of it. An analysis table
(`historical-market-state-analysis-table-v1`, one file per period, opt-in through
`--analysis-table-dir`) holds exactly that part:

- the report's provenance header (period, manifest, coverage, schema, producer
  revision, `report_sha256`, event-time V1 and BOCPD onset versions/hashes, study
  version, `hmm_model_sha256`);
- the Track-A `native_state_quality_summaries`, verbatim;
- the adapted rows (aligned day, Layer-1 contexts, exclusions) of the requested
  configs, without source provenance, which is re-attached from the header when read;
- development periods only: the HMM view (report hash, coverage, the HMM training
  block and its hash, the eight canonical replay scope keys, the V1 continuous
  minute boundaries) and the report side of the EXP-75-09 join.

`derive-table` opens one report, validates it fully once (finalized loader, candidate
registry, sidecar), derives the table and releases it. If the report's sidecar is
missing, `derive-table` creates it from the report it has just validated, as the
full report-validation path does. The table is sealed by `table_sha256` and records
the report's `report_sha256` and its `report_file_sha256` from the sidecar. Every
stored row is checked at derive time to read back from the table with exactly the
canonical JSON spelling it was written with. Tables are create-only; an existing
table must be byte-identical.

EXP-75-09 development rows need the held-out cross-fit folds, which only exist after
all ten development reports. Development tables therefore store only the report side
of that join (on-grid V1 records reduced to their 5-minute classification window,
the matching outcomes and state paths); Part C completes the rows from the folds.
The reduction is self-checked at derive time against the full classification.

Validation tables hold only the development-nominated configs, and test tables only
the authorized members. Both are derived after the same gates as the Part-C phase
runs (frozen parents, HMM prerequisites and, for test, exact authorization with at
least one authorized member), checked before the report is opened.

`verify-table` re-derives a table from its report, requires byte-equality, and
recomputes every stored row through the default adapter path (full report hash, no
index), comparing canonical JSON (so `1` and `1.0` differ) as an audit of the
shortcuts. It never writes: it requires the report's existing sidecar and fails if
it is missing. Every Part-C and HMM artifact and hash is byte-identical whether it
is produced from tables or from the reports.

### Trust model

A table is a self-hashed derived artifact. `table_sha256` proves only that the table
is internally intact. Part C and the HMM tools reading tables check that seal and the
table's frozen identity (manifest, period, producer revision, coverage, HMM
prerequisites), but they do **not** re-check `report_sha256` or `report_file_sha256`
against the Part-B report or its sidecar; they never open either. The binding between
a table and its report is established only by `derive-table`, when the table is
built from a fully validated report, and by `verify-table`, when it is audited
against that report.

Operator step (campaign, not CI): before any phase run with `--analysis-table-dir`,
run `verify-table` on at least one development, one validation and one test period
of the table set (validation and test as soon as they exist). Each run decodes one
full report (~2.5 GB on disk, ~10 GB RSS), so it belongs on campaign runners, never
in CI. Do not use a table set until its sampled periods verify.
