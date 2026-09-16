export const INTERPRETATION_VERSION = "interpretation-v1";

const bullish = new Set(["hammer", "bullish_engulfing", "ema_bullish_cross", "rsi_recovered_30"]);
const bearish = new Set([
  "shooting_star",
  "bearish_engulfing",
  "ema_bearish_cross",
  "rsi_rejected_70",
]);
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** A descriptive rule score for one saved snapshot, never a probability. */
export function interpretTA(price: number, raw: unknown, patterns: string[]) {
  const values: Record<string, unknown> =
    raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
  const fast = values["ema20"],
    slow = values["ema50"],
    rsi = values["rsi14"],
    atr = values["atr14"],
    volume = values["volume_change_pct"];
  const trendValid =
    finite(price) && price > 0 && finite(fast) && fast > 0 && finite(slow) && slow > 0;
  const trendDirection = trendValid
    ? price > fast && fast > slow
      ? 1
      : price < fast && fast < slow
        ? -1
        : 0
    : null;
  const trend =
    trendDirection === null
      ? "Unavailable"
      : trendDirection > 0
        ? "Bullish"
        : trendDirection < 0
          ? "Bearish"
          : "Neutral";
  const momentumValid = finite(rsi) && rsi >= 0 && rsi <= 100;
  const momentum = !momentumValid
    ? "Unavailable"
    : rsi < 30
      ? "Oversold"
      : rsi < 45
        ? "Weak"
        : rsi <= 55
          ? "Neutral"
          : rsi <= 70
            ? "Strong"
            : "Overbought";
  const momentumPoints = !momentumValid
    ? null
    : rsi < 30
      ? -20
      : rsi < 45
        ? -10
        : rsi <= 55
          ? 0
          : rsi <= 70
            ? 10
            : 20;
  const up = patterns.some((p) => bullish.has(p)),
    down = patterns.some((p) => bearish.has(p));
  const direction = up === down ? 0 : up ? 1 : -1;
  const volumeValid = finite(volume) && volume >= -100;
  const aligned = direction !== 0 && direction === trendDirection;
  const support =
    up && down
      ? "Conflicting directions"
      : !up && !down
        ? "No directional pattern"
        : trendDirection === null
          ? "Trend unavailable"
          : !aligned
            ? "Against trend / neutral trend"
            : !volumeValid
              ? "Aligned; volume unavailable"
              : volume < 0
                ? "Aligned; below-average volume"
                : "Aligned + volume supported";
  const contributions = {
    trend: trendDirection === null ? null : trendDirection * 40,
    momentum: momentumPoints,
    patterns: direction * 20,
    volume: volumeValid ? (aligned && volume >= 0 ? direction * 20 : 0) : null,
  };
  const score = Object.values(contributions).every(finite)
    ? Object.values(contributions).reduce<number>((sum, v) => sum + (v ?? 0), 0)
    : null;
  const atrPct = finite(atr) && atr >= 0 && finite(price) && price > 0 ? (atr / price) * 100 : null;
  return {
    trend,
    momentum,
    support,
    contributions,
    score,
    atrPct: atrPct !== null && Number.isFinite(atrPct) ? atrPct : null,
    version: INTERPRETATION_VERSION,
  };
}
