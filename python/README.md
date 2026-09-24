# Isolated Python market analysis

For the new authenticated, read-only FastAPI service and dashboard integration,
see [service setup](../docs/python-api.md). Install `requirements.txt` for the API
and full test suite. The standalone calculation CLI below remains dependency-free.

The new `market_analysis.cumulative.observe` function provides a pure state
transition for cumulative upward/downward monitoring. See the
[application rule and deployment notes](../docs/cumulative-monitoring.md).
The one-shot CLI below still reports rolling-window analysis and does not persist
baselines; its command behavior is unchanged.

`market_analysis.technical.calculate_technical_analysis` is the pure, canonical
TA v2 calculation entry point. Manual analysis, the authenticated single and batch
technical-analysis routes, and `market_analysis.replay` call it with explicit
versioned inputs. These calculation paths do not read a database, use wall-clock
time, or write a result. The replay runner accepts a fixed JSON `TechnicalInput`
fixture:

```sh
.venv/bin/python -m market_analysis.replay path/to/fixture.json
```

It evaluates the fixture chronologically and passes only candles completed at each
evaluation timestamp to the shared calculator.

The calculation core uses only the Python standard library, with no Django,
database client or scheduler. The FastAPI service adds locked HTTP/API dependencies
and a service token. Reference interpreter is CPython 3.10.12 in `.python-version`;
Python 3.10+ syntax is used.

From the repository root:

```sh
cd python
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m market_analysis --symbol BTCUSDT --threshold 2 --window 15
```

The CLI also works with `python3` directly without API dependencies. It performs
a single public-data analysis and exits.
It prints JSON with source, UTC millisecond timestamps, per-window status,
comparison prices, decimal-string percentages, threshold match and provider
attempts. Exit 0 means all windows were computed; exit 1 means no futures provider
supplied valid data; invalid arguments exit 2. An analysis match does not mean an alert
should be created: cooldown is explicitly unchecked. It reads no environment
secrets and never writes results to the app.

See [inspection](INSPECTION.md) for actual application features, write paths and
deployment limitations, and [calculation contract](SPEC.md) for intentional
behavior differences and the remaining product decisions. Fixtures are synthetic,
fixed test data only; the live command never substitutes fixtures.

CLI limits: no historical storage/backtesting, cooldown lookup, result persistence,
or deployed Python service. Sequential HTTP requests use an
8-second timeout per request; there is no scheduler, rate-limit coordinator or
retry loop. It tries Binance USDⓈ-M, OKX USDT swaps, then Kraken perpetual futures.
Local clock accuracy and exchange availability affect results.
