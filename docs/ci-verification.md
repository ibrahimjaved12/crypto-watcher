# PR verification

The `Verify` GitHub Actions workflow runs for every pull request and for pushes
to `main`. A branch with an open PR is checked on its PR event, avoiding a second
push run for the same commit. New pushes cancel older in-progress runs for the
same PR or branch. Both jobs have explicit timeouts and need no repository secrets.

| GitHub check name              | Verification                                                                                                                                                  |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Python tests`                 | Python 3.10, pinned `python/requirements.txt`, and the full `unittest` suite in `python/tests/`.                                                              |
| `Node tests, types and builds` | Node 22 from `.nvmrc`, both npm lockfiles, the full `tests/*.test.mjs` suite, TypeScript typecheck, application build, and standalone collector-worker build. |

The Node suite is defined in `tests/package.json`; adding a `*.test.mjs` file
automatically includes it. The tests use local fixtures, mocked transports and
in-memory PGlite. The workflow only installs dependencies, runs tests, and builds
artifacts. It does not start schedulers, connect to hosted databases or providers,
or run migrations against a hosted database.

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
.venv/bin/python -m unittest discover -s tests -v
```

Once both checks run reliably on normal PRs, add **Python tests** and
**Node tests, types and builds** to the `main` branch's required status checks in
GitHub branch protection. Do not require a check while it is flaky or depends on
an unavailable environment; fix the cause and confirm a green PR run first.
