"""Forward engine (#239 P10): signals, setups, outcomes and a paper-wallet step. Pure and stateless.

No network, no database, no wall clock: every function is a deterministic function of its inputs,
so the TanStack orchestration can re-run any evaluation and persist the results idempotently.
Benchmark modules are only imported, never changed.
"""

VERSIONS = {
    "forward": "forward-v2",
    "bars_adapter": "bars-adapter-v1",
    "signals": "signals-v1",
    "ta": "ta-v1",
    "placebo": "placebo-v1",
    "setups": "setups-v2",
    "labels_schema": "labels-v3",
    "outcomes": "outcomes-v1",
    "wallet": "wallet-v1",
}
