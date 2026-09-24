import type { Candle } from "../market/providers.server";

export const TA_VERSION = "ta-v2";
export const TA_INTERPRETATION_VERSION = "interpretation-v1";
export const TA_FRAMES = [15, 60, 240] as const;
export const TA_MINIMUM_HISTORY = 200;

export function outcomeDue(detectedAt: string, timeframe: number) {
  const duration = timeframe * 60_000;
  return Math.ceil(Date.parse(detectedAt) / duration) * duration + 4 * duration;
}

/** Validates provider data before it crosses the Python service boundary. */
export function completedCandles(input: Candle[], minutes: number, now = Date.now()) {
  const duration = minutes * 60_000;
  const candles = input
    .filter((candle) => candle.complete && candle.time + duration <= now)
    .sort((left, right) => left.time - right.time);
  if (candles.length < TA_MINIMUM_HISTORY) {
    throw new Error(`At least ${TA_MINIMUM_HISTORY} completed candles required`);
  }
  for (const [index, candle] of candles.entries()) {
    if (
      !Number.isSafeInteger(candle.time) ||
      candle.time % duration !== 0 ||
      ![candle.open, candle.high, candle.low, candle.close, candle.volume].every(Number.isFinite) ||
      candle.low <= 0 ||
      candle.volume < 0 ||
      candle.high < Math.max(candle.open, candle.close) ||
      candle.low > Math.min(candle.open, candle.close) ||
      (index > 0 && candle.time - candles[index - 1]!.time !== duration)
    ) {
      throw new Error("Invalid OHLCV, duplicate candle or gap");
    }
  }
  if (now - (candles.at(-1)!.time + duration) > duration + 120_000) {
    throw new Error("Stale TA candles");
  }
  return candles;
}
