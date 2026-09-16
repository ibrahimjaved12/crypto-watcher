import { EMA, RSI, ATR } from "technicalindicators";
import type { Candle } from "../market/providers.server";

export const TA_VERSION = "ta-v1";
export const TA_FRAMES = [15, 60, 240] as const;

export function outcomeDue(detectedAt: string, timeframe: number) {
  const duration = timeframe * 60_000;
  return Math.ceil(Date.parse(detectedAt) / duration) * duration + 4 * duration;
}

export function closedCandles(input: Candle[], minutes: number, now = Date.now()) {
  const duration = minutes * 60_000;
  const candles = input
    .filter((c) => c.complete && c.time + duration <= now)
    .sort((a, b) => a.time - b.time);
  if (candles.length < 200) throw new Error("At least 200 completed candles required");
  for (const [i, c] of candles.entries()) {
    if (
      !Number.isSafeInteger(c.time) ||
      c.time % duration !== 0 ||
      ![c.open, c.high, c.low, c.close, c.volume].every(Number.isFinite) ||
      c.low <= 0 ||
      c.volume < 0 ||
      c.high < Math.max(c.open, c.close) ||
      c.low > Math.min(c.open, c.close) ||
      (i > 0 && c.time - candles[i - 1]!.time !== duration)
    ) {
      throw new Error("Invalid OHLCV, duplicate candle or gap");
    }
  }
  if (now - (candles.at(-1)!.time + duration) > duration + 120_000)
    throw new Error("Stale TA candles");
  return candles;
}

export function patterns(c: Candle, previous: Candle, trend: number): string[] {
  const range = c.high - c.low;
  if (range <= 0) return [];
  const body = Math.abs(c.close - c.open);
  const upper = c.high - Math.max(c.open, c.close);
  const lower = Math.min(c.open, c.close) - c.low;
  const found: string[] = [];
  if (body <= range * 0.1) found.push("doji");
  if (body > range * 0.1 && lower >= body * 2 && upper <= body * 0.5 && trend < 0)
    found.push("hammer");
  if (body > range * 0.1 && upper >= body * 2 && lower <= body * 0.5 && trend > 0)
    found.push("shooting_star");
  if (
    c.close > c.open &&
    previous.close < previous.open &&
    c.open <= previous.close &&
    c.close >= previous.open &&
    body > Math.abs(previous.close - previous.open)
  )
    found.push("bullish_engulfing");
  if (
    c.close < c.open &&
    previous.close > previous.open &&
    c.open >= previous.close &&
    c.close <= previous.open &&
    body > Math.abs(previous.close - previous.open)
  )
    found.push("bearish_engulfing");
  return found;
}

export function analyze(candles: Candle[]) {
  const close = candles.map((c) => c.close);
  const ema20 = EMA.calculate({ period: 20, values: close });
  const ema50 = EMA.calculate({ period: 50, values: close });
  const rsi = RSI.calculate({ period: 14, values: close });
  const atr = ATR.calculate({
    period: 14,
    high: candles.map((c) => c.high),
    low: candles.map((c) => c.low),
    close,
  });
  const last = candles.at(-1)!;
  const average = candles.slice(-21, -1).reduce((sum, c) => sum + c.volume, 0) / 20;
  const detected = patterns(last, candles.at(-2)!, candles.at(-2)!.close - candles.at(-5)!.close);
  if (ema20.at(-2)! <= ema50.at(-2)! && ema20.at(-1)! > ema50.at(-1)!)
    detected.push("ema_bullish_cross");
  if (ema20.at(-2)! >= ema50.at(-2)! && ema20.at(-1)! < ema50.at(-1)!)
    detected.push("ema_bearish_cross");
  if (rsi.at(-2)! < 30 && rsi.at(-1)! >= 30) detected.push("rsi_recovered_30");
  if (rsi.at(-2)! > 70 && rsi.at(-1)! <= 70) detected.push("rsi_rejected_70");
  if (average > 0 && last.volume >= 2 * average) detected.push("volume_spike");
  return {
    candle: { ...last },
    previous_candle: { ...candles.at(-2)! },
    ema20: ema20.at(-1)!,
    ema50: ema50.at(-1)!,
    rsi14: rsi.at(-1)!,
    atr14: atr.at(-1)!,
    volume_change_pct: average > 0 ? (last.volume / average - 1) * 100 : null,
    patterns: detected,
  };
}
