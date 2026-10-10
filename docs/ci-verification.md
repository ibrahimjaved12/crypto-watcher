# PR verification

One GitHub Actions workflow, `CI` (`.github/workflows/verify.yml`), with one job named
`Tests and builds` (the check shows as "CI / Tests and builds"). It runs for pull requests (not for docs-only changes), every Monday as a full run, and on
manual dispatch. There is no push-to-main run: merged code was verified on its PR. New pushes cancel
older in-progress runs for the same PR. The job has a 12-minute timeout and needs no repository
secrets. Budget, tiers and how tests are selected: [ci-budget.md](ci-budget.md).

| Lane   | Verification                                                                                                                                                                      |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python | Python 3.10, pinned `python/requirements.txt`, the `core` tier of `python/tests/` always, plus the `study` tier modules a PR's changed files reach (weekly/dispatch: all tiers). |
| Node   | Node 22 from `.nvmrc`, both npm lockfiles, the full `tests/*.test.mjs` suite, TypeScript typecheck, application build, and standalone collector-worker build.                    |

The lanes run concurrently; a failure in either fails the job and the other lane's log is still
printed. Each run writes a "CI budget" block (seconds per lane, tests run) to the job summary.

The Node suite is defined in `tests/package.json`; adding a `*.test.mjs` file automatically includes
it. The tests use local fixtures, mocked transports and in-memory PGlite. The workflow only installs
dependencies, runs tests, and builds artifacts. It does not start schedulers, connect to hosted
databases or providers, or run migrations against a hosted database.

To reproduce the checks locally, use Node 22 and Python 3.10:

```sh
npm ci
npm ci --prefix tests --ignore-scripts
npm test --prefix tests
npx --no-install tsc --noEmit
npm run build
npm run collector:worker:build
cd python
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python tests/run_shard.py core
.venv/bin/python tests/run_shard.py study        # add study-heavy for the full suite
```

Branch protection is not available on this plan, so no check is "required"; treat a red `CI`
as blocking by convention. Do not rely on a check while it is flaky; fix the cause first.
