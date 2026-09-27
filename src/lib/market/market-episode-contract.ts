/** Transport and persistence contracts for canonical Python #73 output. */
import type {
  ClassifierMetric, ConfirmedMarketDirection, MarketDirectionState, MarketPaceValue,
} from "./market-state-contract";
import type { BreadthSide } from "./movement-metrics-contract";

export const MARKET_EPISODE_ALGORITHM_VERSION = "market-episode-lifecycle-v1";
export const MARKET_EPISODE_STATE_SERIALIZATION_VERSION = "market-episode-state-v1";

/** Opaque Python state. Application code may only return it to Python or persist it. */
export type SerializedMarketEpisodeLifecycleState = {
  serialization_version: typeof MARKET_EPISODE_STATE_SERIALIZATION_VERSION;
  [key: string]: unknown;
};

export type MarketEpisodeTransitionType =
  "STARTED" | "STRENGTHENED" | "WEAKENED" | "REVERSED" | "ENDED";
export type MarketPace = MarketPaceValue | "NOT_APPLICABLE";

export type MarketEpisodeStateSummary = {
  evaluationBoundaryTime: number;
  currentDirectionState: MarketDirectionState;
  currentPace: ClassifierMetric<MarketPaceValue>;
  activeEpisodeId: string | null;
  activeEpisodeDirection: ConfirmedMarketDirection | null;
  interrupted: boolean;
  lifecycleAlgorithmVersion: typeof MARKET_EPISODE_ALGORITHM_VERSION;
  lifecycleConfigVersion: string;
  universeId: string;
  universeVersion: string;
  primaryWindowMinutes: 5;
  classifierAlgorithmVersion: string;
  classifierConfigVersion: string;
  movementAlgorithmVersion: string;
  movementConfigVersion: string;
  provider: "binance-usdm";
  exchange: "binance";
  priceType: "trade";
};

/** Complete Python event is retained verbatim, including nested canonical evidence. */
export type CanonicalMarketEpisodeTransition = {
  event_id: string;
  episode_id: string;
  previous_episode_id: string | null;
  transition: MarketEpisodeTransitionType;
  transition_reason: string;
  from_direction: ConfirmedMarketDirection | null;
  to_direction: ConfirmedMarketDirection | null;
  event_family: "BROAD_MOVE";
  episode_direction: ConfirmedMarketDirection;
  episode_start_boundary_time_ms: number;
  evaluation_boundary_time_ms: number;
  episode_scope: Record<string, unknown>;
  evaluation_scope: Record<string, unknown>;
  episode_lifecycle_config: Record<string, unknown>;
  evaluation_lifecycle_config: Record<string, unknown>;
  windows_context: unknown[];
  source_time_evidence: unknown[];
  directional_breadth: ClassifierMetric<BreadthSide>;
  material_breadth: ClassifierMetric<BreadthSide>;
  median_raw_return: ClassifierMetric<number>;
  median_normalized_movement: ClassifierMetric<number>;
  median_acceleration: ClassifierMetric<number>;
  acceleration_breadth: ClassifierMetric<BreadthSide>;
  pace: ClassifierMetric<MarketPaceValue>;
  dispersion_mad_normalized_movement: ClassifierMetric<number>;
  volume_context: unknown[];
  isolated_outliers: unknown[];
  supporting_contracts: unknown[];
  conflicting_contracts: unknown[];
  configured_universe: string[];
  included_symbols: string[];
  excluded_symbols: unknown[];
  classification: Record<string, unknown>;
};

export type MarketEpisodeLifecycleTransport = {
  serializedState: SerializedMarketEpisodeLifecycleState;
  stateSummary: MarketEpisodeStateSummary;
  transitions: CanonicalMarketEpisodeTransition[];
};
