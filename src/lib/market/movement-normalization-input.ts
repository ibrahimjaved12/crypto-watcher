/** Prepares explicit #71 history inputs from completed operational 1m candles. */
import { MOVEMENT_WINDOWS_MINUTES, type MovementWindowMinutes } from "./movement-contract";
import { DEFAULT_MARKET_MOVEMENT_CONFIG, type HistoricalMovementWindowInput,
  type MarketMovementConfig } from "./movement-metrics-contract";
import type { MovementCandle } from "./market-movement-state";

const MINUTE_MS = 60_000;

export type MovementNormalizationHistory = Map<
  string,
  Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>>
>;

/** A gap truncates the trusted comparable history; duplicate opens use the latest row. */
function trailingContiguousRun(candles: readonly MovementCandle[]): MovementCandle[] {
  const usable = candles
    .filter((candle) =>
      Number.isSafeInteger(candle.openTime) && candle.openTime >= 0 &&
      Number.isFinite(candle.close) && candle.close > 0 &&
      Number.isFinite(candle.volume) && candle.volume >= 0)
    .sort((left, right) => left.openTime - right.openTime);
  const deduped: MovementCandle[] = [];
  for (const candle of usable) {
    const previous = deduped.at(-1);
    if (previous && previous.openTime === candle.openTime) deduped[deduped.length - 1] = candle;
    else deduped.push(candle);
  }
  if (deduped.length === 0) return [];
  let start = deduped.length - 1;
  while (start > 0 && deduped[start]!.openTime - deduped[start - 1]!.openTime === MINUTE_MS) {
    start -= 1;
  }
  return deduped.slice(start);
}

export function buildMovementNormalizationHistory(
  candlesBySymbol: ReadonlyMap<string, readonly MovementCandle[]>,
  config: MarketMovementConfig = DEFAULT_MARKET_MOVEMENT_CONFIG,
): MovementNormalizationHistory {
  const result: MovementNormalizationHistory = new Map();
  for (const [symbol, candles] of candlesBySymbol) {
    result.set(symbol.toUpperCase(), buildSymbolNormalization(candles, config));
  }
  return result;
}

/** Completed-candle closes represent the boundary one minute after their open. */
function buildSymbolNormalization(
  candles: readonly MovementCandle[],
  config: MarketMovementConfig,
): Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>> {
  const windows: Partial<Record<MovementWindowMinutes, HistoricalMovementWindowInput>> = {};
  const run = trailingContiguousRun(candles);
  if (run.length < 2) return windows;
  const usableCoverageMs = run.at(-1)!.openTime + MINUTE_MS - run[0]!.openTime;
  const oldestOpenTime = run[0]!.openTime;
  const byOpenTime = new Map(run.map((candle) => [candle.openTime, candle]));

  for (const windowMinutes of MOVEMENT_WINDOWS_MINUTES) {
    const windowMs = windowMinutes * MINUTE_MS;
    const returns: number[] = [];
    const previousNotionalVolumes: number[] = [];
    for (const current of run) {
      const boundary = current.openTime + MINUTE_MS;
      if (boundary % windowMs !== 0) continue;
      const priorBoundary = boundary - windowMs;
      const previousOpenTime = priorBoundary - MINUTE_MS;
      if (previousOpenTime < oldestOpenTime) continue;
      const previous = byOpenTime.get(previousOpenTime);
      if (!previous) continue;
      let notional = 0;
      let contiguous = true;
      for (let openTime = priorBoundary; openTime <= boundary - MINUTE_MS; openTime += MINUTE_MS) {
        const candle = byOpenTime.get(openTime);
        if (!candle) {
          contiguous = false;
          break;
        }
        notional += candle.close * candle.volume;
      }
      if (!contiguous) continue;
      returns.push(Math.log(current.close / previous.close));
      previousNotionalVolumes.push(notional);
    }
    if (returns.length === 0) continue;
    windows[windowMinutes] = {
      returns,
      usableCoverageMs,
      previousNotionalVolumes: previousNotionalVolumes.slice(-config.rvolComparisonWindows),
    };
  }
  return windows;
}
