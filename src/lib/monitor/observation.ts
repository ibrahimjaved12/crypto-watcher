import type { Candle } from "../market/providers.server";

/** Select an actual completed 1m close, never a forming candle or future time. */
export function completedObservation(candles: Candle[], now = Date.now()) {
  const seen = new Set<number>();
  for (const candle of candles) {
    if (
      !Number.isSafeInteger(candle.time) ||
      candle.time < 0 ||
      candle.time % 60_000 !== 0 ||
      candle.time > now ||
      seen.has(candle.time) ||
      !Number.isFinite(candle.close) ||
      candle.close <= 0
    ) {
      throw new Error("Invalid or duplicate candle data");
    }
    seen.add(candle.time);
  }
  const completed = candles.filter((c) => c.complete && c.time + 60_000 <= now);
  const last = completed.reduce<Candle | undefined>(
    (a, b) => (!a || b.time > a.time ? b : a),
    undefined,
  );
  if (!last || now - (last.time + 60_000) > 10 * 60_000) {
    throw new Error("No fresh completed 1m candle");
  }
  return { price: last.close, observedAt: new Date(last.time + 60_000).toISOString() };
}
