# CI budget (P19, Refs #181)

GitHub bills each job rounded **up** to whole minutes, so billed minutes = the sum over jobs of
`ceil(job minutes)`. The private repo's monthly allowance was being spent on tests alone.

## Before (measured 2026-10-10, run 38035890716 on d61643b)

| job | wall | billed |
|---|---|---|
| Python · study part C | 113 s | 2 |
| Python · study core | 75 s | 2 |
| Python · benchmark and everything else | 143 s | 3 |
| Python tests (aggregator, only echoes) | 3 s | 1 |
| Node | 95 s | 2 |
| **one run** | | **10** |

`Verify` ran 206 times in 7 days (every push to a PR branch and every push to main): about 2,000
billed minutes.

## After (targets, to be confirmed on the first CI run)

One job `Verify`, three lanes sharing one checkout and one setup:

| case | wall | billed |
|---|---|---|
| typical PR (no study code touched) | <= 100 s | 2 |
| PR that touches study code | <= 170 s | 3 |
| docs-only PR (`docs/**`, `**/*.md`, `research/**`) | no run | 0 |

## What runs when

- **Pull request:** core Python tier, the Node tests + `tsc` + both builds, and only the study-tier
  modules whose imports reach a changed file (`python/tests/select_tests.py`). Python and Node run
  concurrently on the two vCPUs; one failing lane never hides the other.
- **Weekly (Monday 04:41 UTC) and manual dispatch:** everything, including the whole study tier.
- **No push-to-main run:** merged code was verified on its PR; the weekly run is the net.
- Changes to `python/requirements*.txt`, `run_shard.py`, `shards.json`, `select_tests.py`, the
  fixtures directory or `verify.yml` select the whole study tier. Dynamic imports (`importlib`) are
  invisible to the selector; the weekly run catches them.
- Every run writes a "Verify budget" block to `$GITHUB_STEP_SUMMARY`: seconds per lane, the
  `run_shard:` lines (tier, modules, tests, failed, seconds, or "skipped (no study code touched)") and
  the Node "N/M files passed" line.

## Adding a test

- Put it in `python/tests/` as usual: **core by default** (every module not listed in `shards.json`).
- List a module under `"study"` in `python/tests/shards.json` only for the #123/EXP-75 research
  cluster (`historical_*`, `experiments/market_state_*`, `replay`). The fate of #123/#152/#163 is an
  open owner decision, so those tests are gated, not removed.
- Heavy statistical trials use `@slow` from `python/tests/slow.py` and keep a small deterministic case
  in the default suite. `test_benchmark_spa.test_null_calibration` and
  `test_benchmark_stepm.test_all_noise_rejects_nothing_mostly` are `@slow` now (run them with
  `RUN_SLOW_TESTS=1`; `slow-tests.yml` currently runs only `test_benchmark_calibration`).
- `python tests/run_shard.py core|study [--changed-file FILE] [--jobs N]` prints a per-module seconds
  table (slowest first). A new module over ~10 s needs a reason.
- Node test files run in a bounded-parallel pool (`tests/run-tests.mjs`, `TEST_JOBS` or CPU count).

## Pruning standard for new and existing tests

Delete a test when it (a) repeats another test through a different route, (b) asserts source text,
file contents, doc wording or a schema's literal field list instead of behaviour, (c) tests code that
no longer exists, (d) is a differential test against a verbatim copy of an old implementation whose
fast path is gone, (e) pins the bytes of a retired report format, (f) enumerates a large matrix where
3-5 boundary cases say the same, (g) re-asserts a constant the code under test already defines.
Never weaken: no look-ahead / point-in-time, the label engine (ordering, ambiguity, costs, funding,
liquidation), wallet maths, the hidden-stretch guard, stale-data handling, candle immutability and
`candle_version`, SPA/StepM/DSR/placebo correctness, idempotent persistence, boundary validation. A test
that cannot fail, or recomputes its expectation with the code under test, is weak: give it an
independent expected value or delete it.

## Proposed replacement for the CI bullets in `CLAUDE.md` section 6 (proposal; CLAUDE.md is not edited here)

> - Python CI budget: `Verify` is one job (about 2 billed minutes per PR). New tests go to the `core`
>   tier by default and should stay under ~10 s; the research cluster is the `study` tier in
>   `python/tests/shards.json` and runs on PRs only when its imports are touched, plus weekly.
>   Heavy statistical acceptance tests use `@slow` (slow-tests.yml). Do not add CI jobs or push
>   triggers without checking `docs/ci-budget.md`.
