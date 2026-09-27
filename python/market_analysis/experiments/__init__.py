"""Research-only market-state experiments.

Experiment modules are pure calculation code and are intentionally not imported
by the live movement service or API.
"""

from .market_state_ewma import (
    EWMA_ALGORITHM_VERSION,
    EWMA_CONFIG_10S,
    EWMA_CONFIG_30S,
    EWMA_CONFIG_60S,
    EWMA_CONFIGURATIONS,
    EWMA_HALF_LIVES_MS,
    EWMAConfig,
    EWMACandidateState,
    ExperimentPartition,
    ExperimentComparisonSummary,
    MarketStateEWMAExperimentResult,
    MarketStateExperimentPoint,
    PairedMarketStateExperimentPoint,
    run_market_state_ewma_experiment,
    transform_market_movement_with_ewma,
)

__all__ = [
    "EWMA_ALGORITHM_VERSION",
    "EWMA_CONFIG_10S",
    "EWMA_CONFIG_30S",
    "EWMA_CONFIG_60S",
    "EWMA_CONFIGURATIONS",
    "EWMA_HALF_LIVES_MS",
    "EWMAConfig",
    "EWMACandidateState",
    "ExperimentPartition",
    "ExperimentComparisonSummary",
    "MarketStateEWMAExperimentResult",
    "MarketStateExperimentPoint",
    "PairedMarketStateExperimentPoint",
    "run_market_state_ewma_experiment",
    "transform_market_movement_with_ewma",
]
