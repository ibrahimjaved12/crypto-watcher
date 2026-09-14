# Isolated Python market analysis

Uses only the Python standard library; no Django, database client, scheduler,
credentials or third-party dependencies. Reference interpreter is CPython 3.10.12
in `.python-version`; Python 3.10+ syntax is used. `requirements.txt` explicitly
records the empty dependency set, so no package resolution is required.

From the repository root:

```sh
cd python
python3 -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m market_analysis --symbol BTCUSDT --threshold 2 --window 15
```

The same commands work with `python3` directly; the venv is optional because there
are no dependencies. The CLI performs a single public-data analysis and exits.
It prints JSON with source, UTC millisecond timestamps, per-window status,
comparison prices, decimal-string percentages, threshold match and fallback
attempts. Exit 0 means all windows were computed; exit 1 means no provider supplied
valid data; invalid arguments exit 2. An analysis match does not mean an alert
should be created: cooldown is explicitly unchecked. It reads no environment
secrets and never writes results to the app.

See [inspection](INSPECTION.md) for actual application features, write paths and
deployment limitations, and [calculation contract](SPEC.md) for intentional
behavior differences and the remaining product decisions. Fixtures are synthetic,
fixed test data only; the live command never substitutes fixtures.

## Validation performed

- CPython 3.10.12: 23 offline tests passed. Includes exact close boundaries, missing
  baselines/internal gaps, stale limits for both intervals, nonfinite prices,
  ordering/duplicates, up/down moves, decimal threshold boundaries, provider
  normalization, fallback order and structured total failure.
- Live `BTCUSDT --threshold 2 --window 15`: exit 0, Binance, all five windows valid.
  Network access required permission outside the sandbox. Live OKX/Kraken and all
  allowlisted pairs were not tested; their parsers/fallback behavior use fixtures.
- `git diff --check` passed. Existing frontend `npm run build` failed before
  compilation in Rolldown's `styleText` on Node 21.7.1; installed Vite/Rolldown
  declare `^20.19.0 || >=22.12.0`. Re-run with a supported Node runtime. No frontend
  source or dependency files were changed.

Remaining limits: no historical storage/backtesting, cooldown lookup, trusted
result ingestion or deployed Python service. Sequential HTTP requests use an
8-second timeout per request; there is no scheduler, rate-limit coordinator or
retry loop. Provider fallback is the only retry mechanism. Local clock accuracy
and exchange endpoint availability affect results.
