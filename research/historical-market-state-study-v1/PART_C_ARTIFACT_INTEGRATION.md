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
