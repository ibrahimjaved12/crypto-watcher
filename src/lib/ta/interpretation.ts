export const INTERPRETATION_VERSION = "interpretation-v1";

const bullish = new Set(["hammer", "bullish_engulfing", "ema_bullish_cross", "rsi_recovered_30"]);
const bearish = new Set([
  "shooting_star",
  "bearish_engulfing",
  "ema_bearish_cross",
  "rsi_rejected_70",
]);
const finite = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

const record = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
const display = (v: unknown) => (finite(v) ? Number(v.toPrecision(6)).toString() : "unavailable");
const patternRules: Record<string, string> = {
  doji: "Body is at most 10% of the candle range; describes indecision, without a direction.",
  hammer:
    "Lower wick is at least twice the body, upper wick at most half the body, after three-bar decline.",
  shooting_star:
    "Upper wick is at least twice the body, lower wick at most half the body, after three-bar rise.",
  bullish_engulfing:
    "A bullish body fully covers the preceding bearish body and is strictly larger.",
  bearish_engulfing:
    "A bearish body fully covers the preceding bullish body and is strictly larger.",
  ema_bullish_cross: "EMA20 crossed above EMA50 on this completed candle.",
  ema_bearish_cross: "EMA20 crossed below EMA50 on this completed candle.",
  rsi_recovered_30: "RSI moved from below 30 to at least 30.",
  rsi_rejected_70: "RSI moved from above 70 to at most 70.",
  volume_spike: "Volume is at least twice the mean of the preceding 20 candles.",
};

/** Descriptive context only: correlated indicators are not added to the v1 score. */
export function explainTA(price: number, raw: unknown, patterns: string[]) {
  const v = record(raw);
  const m = record(v["macd"]),
    b = record(v["bollinger"]),
    range = record(v["range20"]);
  const ema = v["ema200"],
    histogram = m["histogram"],
    previous = m["previous_histogram"];
  const adx = v["adx14"],
    low = range["low"],
    high = range["high"];
  const validPrice = finite(price) && price > 0;
  const longTrend =
    validPrice && finite(ema) && ema > 0
      ? price > ema
        ? "Bullish"
        : price < ema
          ? "Bearish"
          : "Neutral"
      : "Unavailable";
  const macdBias = finite(histogram)
    ? histogram > 0
      ? "Bullish"
      : histogram < 0
        ? "Bearish"
        : "Neutral"
    : "Unavailable";
  const bandPosition =
    validPrice && finite(b["upper"]) && finite(b["lower"])
      ? price > b["upper"]
        ? "Above upper band"
        : price < b["lower"]
          ? "Below lower band"
          : "Inside bands"
      : "Unavailable";
  const strength =
    finite(adx) && adx >= 0 && adx <= 100
      ? adx >= 25
        ? "Trending"
        : adx < 20
          ? "Weak trend"
          : "Developing trend"
      : "Unavailable";
  const rangePosition =
    validPrice && finite(low) && finite(high)
      ? price > high
        ? "Above prior range"
        : price < low
          ? "Below prior range"
          : "Inside prior range"
      : "Unavailable";
  return [
    {
      label: "EMA 20 / 50 / 200",
      value: `${display(v["ema20"])} / ${display(v["ema50"])} / ${display(ema)}`,
      explanation: `Short trend is bullish when close > EMA20 > EMA50, bearish when close < EMA20 < EMA50, otherwise neutral (including equality). EMA200 context: ${longTrend}, comparing close ${display(price)} with EMA200. EMA periods are candles in the selected timeframe; finite-history initialization can affect long averages.`,
    },
    {
      label: "RSI 14",
      value: display(v["rsi14"]),
      explanation:
        "Wilder RSI: below 30 oversold; 30 to below 45 weak; 45–55 neutral; above 55 through 70 strong; above 70 overbought. Extremes are momentum descriptions, not automatic reversal signals.",
    },
    {
      label: "MACD 12 / 26 / 9",
      value: `${macdBias} · line ${display(m["line"])} · signal ${display(m["signal"])} · histogram ${display(histogram)}`,
      explanation: `MACD is EMA12 minus EMA26; signal is its EMA9; histogram is line minus signal. Positive histogram gives bullish momentum bias, negative bearish. ${finite(histogram) && finite(previous) ? `Histogram is ${histogram > previous ? "rising" : histogram < previous ? "falling" : "unchanged"} from ${display(previous)}.` : "Previous histogram unavailable."} This is separate from the MACD line's position relative to zero.`,
    },
    {
      label: "Bollinger Bands 20 / 2",
      value: `${bandPosition} · lower ${display(b["lower"])} · middle ${display(b["middle"])} · upper ${display(b["upper"])}`,
      explanation: `20-close SMA ± two population standard deviations. Bandwidth: ${display(b["bandwidth_pct"])}%; %B: ${display(b["percent_b"])} (0 lower band, 1 upper band). Outside-band closes do not guarantee reversal. %B is unavailable when bands coincide.`,
    },
    {
      label: "ATR 14 / volatility",
      value: display(v["atr14"]),
      explanation: `Wilder average true range in quote-price units; ATR/close ×100 = ${validPrice && finite(v["atr14"]) ? display((v["atr14"] / price) * 100) : "unavailable"}%. Measures movement size, not direction; compare like timeframes.`,
    },
    {
      label: "Volume vs previous 20 candles",
      value: `${display(v["volume_ratio"])}× · change ${display(v["volume_change_pct"])}%`,
      explanation: `Latest completed base-asset volume divided by the prior 20-candle mean (${display(v["volume_average20"])}), excluding the latest candle. 1× is average, 2× is a spike; zero reference volume is unavailable. Volume alone gives no direction.`,
    },
    {
      label: "ADX 14 / trend strength",
      value: `${strength} · ${display(adx)} · +DI ${display(v["plus_di14"])} / −DI ${display(v["minus_di14"])}`,
      explanation:
        "Wilder ADX measures strength, not direction. Below 20 weak, 20–25 developing, at least 25 trending: heuristic boundaries. +DI/−DI describe directional movement. Flat data may have undefined ADX.",
    },
    {
      label: "Prior 20-candle range",
      value: `${rangePosition} · low ${display(low)} · high ${display(high)}`,
      explanation:
        "Lowest low and highest high of the preceding 20 completed candles, excluding the latest. These are reference levels, not guaranteed support/resistance; only a strictly outside close is labeled a range break.",
    },
    ...patterns.map((pattern) => ({
      label: pattern.replaceAll("_", " "),
      value: "Detected",
      explanation: patternRules[pattern] ?? "No explanation available for this historical pattern.",
    })),
  ];
}

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
