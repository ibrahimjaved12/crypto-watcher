# Crypto Watcher

The dashboard now supports optional read-only Python analysis through a separate
FastAPI service. See [Python API setup and hosted testing](docs/python-api.md)
for the server secrets, local/Docker commands and deployment steps.

The monitoring algorithm now has a [saved-baseline cumulative rule](docs/cumulative-monitoring.md)
for gradual rises and falls. Its database migration must be applied before deploying
the updated monitor; the original milestone brief below remains as project history.

Build a personal crypto monitoring web app called Crypto Watch.

Overall goal:

Monitor market movements, show technical analysis and relevant news,

send alerts, and preserve analysis and hypothetical trade outcomes.

All actual trading stays manual. Do not implement trade execution.

Build this first milestone:

A working market dashboard with persistent storage and scheduled

price monitoring.

Platform:

- Use Lovable Cloud for the database, authentication and backend.

- Require login and restrict each user's records to that user.

- Store records in actual database tables, not browser localStorage.

- Keep API secrets on the backend.

- Use a clean, responsive dark dashboard.

Watchlist and dashboard:

- Start with BTCUSDT, ETHUSDT and DOGEUSDT.

- Allow adding/removing supported trading pairs, up to 10.

- Fetch real public exchange prices and candles.

- Show current price and changes over 5m, 15m, 1h, 4h and 24h.

- Show a price chart, last successful update and data-source status.

- Clearly show unavailable or stale data. Never invent market data.

Monitoring:

- Implement server-side scheduled checks, initially every 5 minutes,

  so monitoring continues when my browser is closed.

- Confirm the platform supports this schedule and explain any limits.

- Make the price-change threshold and time window configurable.

- Save an alert when a threshold is crossed.

- Add a 15-minute cooldown per symbol and rule to prevent duplicates.

- Record failed checks and the last successful monitoring run.

- If the data provider is inaccessible from the hosting region,

  report that clearly instead of substituting simulated prices.

Persistence:

- Save watchlists, settings, alerts and personal notes.

- Each alert should include symbol, timestamp, observed change,

  comparison window, triggering rule and data source.

- Include searchable alert history and CSV export.

- Include a test-alert action, clearly marked as test data.

Future milestones:

Technical indicators and candidate setups; news collection and AI

summaries; email alerts; hypothetical entry/TP/SL outcomes and

performance reporting; WhatsApp later.

Keep the code modular for these additions, but implement only the

first milestone now. Explain what works, what needs configuration,

and any ongoing usage costs. Do not present placeholders as completed

integrations.

This project was built with [Lovable](https://lovable.dev).

## Build with Lovable

Continue developing this project in the [Lovable editor](https://lovable.dev/projects/68fe2179-9a39-4ad0-9473-db2bcbcc8112).

- **Ship faster**: describe what you want to build and Lovable handles the code.
- **Stay in sync**: every change made in Lovable is committed straight to this repository.
- **Full ownership**: this code is yours. Push to `main` on GitHub and your changes sync back into Lovable, ready for your next prompt.

## Development

Prefer working locally? You need Node.js and npm — [install with nvm](https://github.com/nvm-sh/nvm#installing-and-updating).

```sh
git clone <this-repository-url>
cd <repository-name>
nvm install
nvm use
npm i
npm run dev
```

The repository pins Node 22 in `.nvmrc`; use that version for development and
production builds.

## Python analysis milestone

The separate [Python module](python/README.md) provides a one-shot public-candle
analysis command and offline fixture tests. It does not change the running monitor,
authentication, database or scheduler. Read the [implementation inspection](python/INSPECTION.md)
and [calculation specification](python/SPEC.md) before integrating its results.
