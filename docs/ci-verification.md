# PR verification

One GitHub Actions workflow, `Tests` (`.github/workflows/verify.yml`), with one job named
`Python tests, Node tests and builds` (the check shows as "Tests / Python tests, Node tests and builds"). It runs for pull requests (not for docs-only changes), every Monday as a full run, and on
manual dispatch. There is no push-to-main run: merged code was verified on its PR. New pushes cancel
older in-progress runs for the same PR. The job has a 12-minute timeout and needs no repository
secrets. A typical PR run should finish in under 2 minutes. Budget, tiers and how tests are
selected: [ci-budget.md](ci-budget.md).

| Lane   | Verification                                                                                                                                                                      |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python | Python 3.10, pinned `python/requirements.txt`, the `core` tier of `python/tests/` (foundation + live product + forward, run as two binned `unittest` processes) always, plus the `study` tier (research machinery) modules a PR's changed files reach (weekly/dispatch: all tiers). |
| Node   | Node 22 from `.nvmrc`, both npm lockfiles, the full `tests/*.test.mjs` suite, TypeScript typecheck, application build, and standalone collector-worker build.                    |

The lanes run concurrently; a failure in either fails the job and the other lane's log is still
printed. Each run writes a "Test budget" block (seconds per lane, tests run) to the job summary.

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

Branch protection is not available on this plan, so no check is "required"; treat a red `Tests`
as blocking by convention. Do not rely on a check while it is flaky; fix the cause first.

## Lighthouse

**When.** When a PR opens and on every subsequent PR commit, if it touches `src/**`, `public/**`, `vite.config.ts`, `package.json`,
`package-lock.json`, `wrangler.jsonc`, the workflow or its two scripts. To refresh the comment later, run the
workflow manually with the PR number (`pr_number`). Informational only: no thresholds, no failing gate.

**What.** The fixed production URL (`BASE_URL`), so the scores describe deployed `main`, not the PR. Six
logged-in pages (Dashboard, Alerts, Strategy Lab, Notes, Paper account, Settings) for Crypto and Forex, on
mobile and desktop: one job, one install, one sign-in and all 24 runs in one Chrome. Lighthouse audits one URL
per run and does not crawl. A host check runs first; if the site does not answer, the comment says "Host server
is down" and nothing else runs. The PR gets one sticky comment (`<!-- lighthouse-check -->`); full HTML/JSON
reports, final screenshots (all pages except Settings, which shows the account email) and `scores.json` are one
run artifact, `lighthouse-reports` (14 days). The step summary also lists the run time per run.

**Caches.** Every run starts from a cold HTTP cache and cleared service workers/Cache Storage (login and market
stay in localStorage; the app stores no other data there), with a fresh Chrome profile per job. Cloudflare's edge
cache and server-side caches are intentionally not bypassed because real visitors get them, so the first run
after a deploy can be slightly slower.

**Notes.** Two status notes exist besides the sign-in ones: "Host server is down" (the Cloudflare site is
unreachable, nothing runs) and "Runner is offline" (the laptop runner, read from the app's Runner badge; pages
are still measured, with the app's offline banner showing)..

**Setup.**
- Supabase dashboard > Authentication > Users > Add user: use a non-personal email, a generated
  password of at least 32 characters, and auto-confirm.
- On your own machine, run `npm run access:grant -- <that email> --label all --expires <date about a year out>`.
  It reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from `.env.production.local`; those values
  must never go into GitHub.
- GitHub: Settings > Secrets and variables > Actions > add `LIGHTHOUSE_EMAIL` and
  `LIGHTHOUSE_PASSWORD`.
- Comments may say: "Test sign-in is not set up." (no secrets), "Test sign-in failed." (wrong
  password or expired account), "Test account has no access to Crypto/Forex." (label), or
  "Test account has no market access." (no label at all).

**Security.**
- A dedicated, empty test account with the label `all` and an expiry. Never a real account, never the
  service-role key in GitHub.
- Secrets reach only the single run step and are masked. Fork PRs get no secrets and every job skips them
  (never `pull_request_target`).
- Artifacts of a public repo can be downloaded by any signed-in GitHub user, so the account must stay empty;
  reports are made with screenshots disabled.

**Too slow?** Remove lines from `PAGES` in `.github/workflows/lighthouse.yml`.

**Previews.** When #290 provides preview URLs, change `BASE_URL` to use them.
