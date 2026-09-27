/** Raw canonical completed candles transported to Python #71. */
import type { MovementCandle } from "./market-movement-state";

export type MovementRawHistory = Map<string, readonly MovementCandle[]>;
export type MovementInstrumentCompatibility = Map<string, boolean | null>;
