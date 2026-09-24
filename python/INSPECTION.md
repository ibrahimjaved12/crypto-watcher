# Python integration boundaries

The Python package owns deterministic analysis calculations. It has no application
database client, user-session handling, scheduler, baseline persistence, alert
writer, or TA writer.

TanStack owns:

- authenticated manual actions and the shared-secret scheduled route;
- account/watchlist boundaries and activity controls;
- due completed-candle selection and public futures candle loading;
- strict validation of Python responses; and
- the sole privileged database write path.

Scheduled TA sends versioned batches of one to eight due snapshots to
`POST /v1/technical-analysis/batch`. Manual analysis uses `POST /v1/analysis`.
Both routes call `market_analysis.technical.calculate_technical_analysis`, which is
also imported by chronological replay. Python service failures are reported by the
monitor and never invoke another calculator.

Movement collection, cumulative baselines, and movement alerts remain in the
TanStack/PostgreSQL path. The shared Python cumulative function is used for the
read-only manual preview; it does not persist state.

Deployment still requires a reachable Python service, matching service token,
`PYTHON_ANALYSIS_ENABLED=true`, a valid HTTPS origin (or loopback HTTP locally),
and the existing TanStack/Supabase server configuration. Repository code does not
prove that an external scheduler or hosted service is currently enabled or healthy.
