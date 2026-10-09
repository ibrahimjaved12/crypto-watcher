/**
 * Central translation layer: machine values (statuses, reasons, strategy ids, outcome codes) to plain
 * words for the UI. Display only — canonical values are never changed in storage, requests or
 * contracts; callers keep the raw value for a "Details" disclosure. No imports, so it can be loaded
 * by the Node tests as plain transpiled TypeScript.
 */

export type Level = "ok" | "wait" | "problem";
export type Reason = { short: string; help?: string | undefined; level?: Level | undefined };

/** "some_new_reason" / "SOME-REASON" -> "Some new reason". Never throws. */
export function humanizeCode(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "Unavailable";
  const words = value.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  const text = words === words.toUpperCase() ? words.toLowerCase() : words;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const EXACT: Record<string, Reason> = {
  // Run / evaluation statuses (monitor runs, forward jobs, trend track).
  ok: { short: "Completed", level: "ok" },
  success: { short: "Completed", level: "ok" },
  partial: { short: "Completed with issues", level: "wait",
    help: "Some parts finished; the rest will be retried on the next run." },
  failed: { short: "Failed", level: "problem" },
  skipped: { short: "Skipped", level: "wait", help: "Nothing to do: monitoring is paused or no pairs are watched." },
  already_done: { short: "Already up to date", level: "ok", help: "This period was already processed; nothing new was added." },
  running: { short: "In progress", level: "wait" },
  skipped_stale: {
    short: "Skipped: market data was too old to trust",
    help: "No trades were simulated for this hour because the latest candles were missing or delayed. This is the safe behaviour; it recovers by itself once fresh data arrives.",
    level: "wait",
  },
  funding_unavailable: {
    short: "Funding rates could not be fetched",
    help: "Days cannot be scored without the exchange's funding payments, so they stay open (never filled with zero). The next run retries.",
    level: "wait",
  },
  // Data-provider failure categories (src/lib/analysis.contract.ts).
  provider_timeout: { short: "The exchange did not answer in time", level: "problem",
    help: "Usually temporary. Try again in a minute." },
  provider_unavailable: { short: "The exchange could not be reached", level: "problem",
    help: "All configured exchanges failed or were unreachable from the server." },
  invalid_or_stale_data: { short: "Exchange data was invalid or out of date", level: "problem" },
  invalid_data: { short: "Exchange returned invalid data", level: "problem" },
  stale_data: { short: "Exchange data was out of date", level: "wait" },
  insufficient_history: { short: "Not enough past candles yet for this timeframe", level: "wait",
    help: "Indicators need a minimum number of completed candles. This resolves as history accumulates." },
  // Technical-analysis and rolling-window statuses.
  insufficient: { short: "Not enough history yet", level: "wait" },
  unavailable: { short: "Unavailable", level: "problem" },
  stale: { short: "Out of date", level: "wait" },
  // Collector health.
  live: { short: "Live", level: "ok" },
  recovering: { short: "Catching up", level: "wait" },
  lagging: { short: "Lagging", level: "wait" },
  pending: { short: "Not yet known", level: "wait" },
};

const PATTERNS: [RegExp, Reason][] = [
  [/constraint|could not.*sav|failed.*sav|insert.*fail|database|\bDB\b/i, {
    short: "A record could not be saved", level: "problem",
    help: "A database write failed. The details below show which record and why.",
  }],
  [/ta_signals_futures_provenance_check|provenance/i, {
    short: "A saved signal was rejected because its data source details were incomplete",
    help: "Internal consistency check on where the candles came from; the record was not stored.",
    level: "problem",
  }],
  [/catch-?up gap/i, {
    short: "Technical analysis could not catch up",
    help: "The gap since the last run is longer than the stored candle history. It recovers as new candles arrive.",
    level: "wait",
  }],
  [/skipped_stale/i, EXACT["skipped_stale"]!],
  [/funding_unavailable|funding/i, EXACT["funding_unavailable"]!],
  [/collector (missing|stale|recovering|unavailable)|no candles|last completed minute missing|gap\(s\) in candle history/i, {
    short: "Waiting for market data from the collector",
    help: "The background collector has not stored enough recent one-minute candles for every pair. Start or restart the collector; it backfills by itself.",
    level: "wait",
  }],
  [/insufficient_history|insufficient history/i, EXACT["insufficient_history"]!],
  [/incomplete startup|daily kline fetch failed|history incomplete|daily bars not yet complete/i, {
    short: "Daily price history is not complete yet",
    help: "The daily portfolios start only when every pair has full daily history. The next run retries.",
    level: "wait",
  }],
  [/daily bar conflict/i, {
    short: "A stored daily candle differs from the exchange",
    help: "The stored value was kept and nothing was overwritten. This needs a manual look.",
    level: "problem",
  }],
  [/timeout|timed out/i, EXACT["provider_timeout"]!],
  [/rate limited|HTTP 4(18|29)/i, { short: "The exchange rate-limited the request", level: "wait",
    help: "Too many requests in a short time. The next run retries." }],
  [/stale/i, { short: "Market data was out of date", level: "wait" }],
];

/** Plain words for any machine status/reason. Unknown values fall back to a humanised sentence. */
export function humanizeReason(raw: string | null | undefined): Reason {
  if (typeof raw !== "string" || !raw.trim()) return { short: "No details" };
  const key = raw.trim().toLowerCase();
  if (Object.prototype.hasOwnProperty.call(EXACT, key)) return EXACT[key]!;
  for (const [pattern, reason] of PATTERNS) if (pattern.test(raw)) return reason;
  return { short: humanizeCode(raw.length > 160 ? `${raw.slice(0, 157)}…` : raw) };
}

// ---------------------------------------------------------------- strategies

export type StrategyInfo = { name: string; blurb: string };

const TA: Record<string, StrategyInfo> = {
  ema_cross_20_50: { name: "EMA 20/50 crossover", blurb: "Goes with the trend when the fast average crosses the slower one." },
  ema_cross_50_200: { name: "EMA 50/200 crossover", blurb: "Slow trend switch (\"golden/death cross\")." },
  rsi_14_reversion: { name: "RSI 14 reversal", blurb: "Bets on a bounce after RSI reaches oversold (<30) or overbought (>70)." },
  macd_12_26_9: { name: "MACD 12/26/9 crossover", blurb: "Momentum turn when MACD crosses its signal line." },
  bollinger_20_2_reversion: { name: "Bollinger 20/2 reversal", blurb: "Bets on a return to the average after price closes outside the bands." },
  keltner_breakout_20_14_2: { name: "Keltner 20/14/2 breakout", blurb: "Follows a close outside the volatility channel." },
};

const TREND: Record<string, StrategyInfo> = {
  ens_ls_25: { name: "Trend ensemble · long/short · 25% vol", blurb: "Nine breakout channels (5–360 days) vote; position sized for 25% yearly volatility." },
  ens_lo_25: { name: "Trend ensemble · long only · 25% vol", blurb: "Same vote, but never short: flat instead." },
  ens_ls_15: { name: "Trend ensemble · long/short · 15% vol", blurb: "Same vote with smaller positions (15% yearly volatility)." },
  ens_ls_25_sub3: { name: "Trend ensemble · 3 channels · 25% vol", blurb: "Only the 20, 60 and 150-day channels vote." },
  tsmom_7: { name: "7-day momentum", blurb: "Long if the last 7 days were up, short if down." },
  tsmom_14: { name: "14-day momentum", blurb: "Long if the last 14 days were up, short if down." },
  tsmom_28: { name: "28-day momentum", blurb: "Long if the last 28 days were up, short if down." },
  ens_ls_25_mfilter: { name: "Trend ensemble · moving-average filter", blurb: "Trades only in the direction the 20–200 day averages agree on." },
  ens_ls_25_rvfilter: { name: "Trend ensemble · calm-market filter", blurb: "Opens no new position when recent volatility is unusually high." },
  bh_vt_std90_25: { name: "Buy & hold · 25% vol (benchmark)", blurb: "Always long, sized like the trend portfolios; what the trend must beat." },
  bh_vt_std90_15: { name: "Buy & hold · 15% vol (benchmark)", blurb: "Always long with smaller positions; benchmark for the 15% variant." },
  bh_vt_ewma60_25: { name: "Buy & hold · momentum sizing (benchmark)", blurb: "Always long, sized like the momentum portfolios." },
  ew_long: { name: "Equal-weight long (benchmark)", blurb: "Simply holds every pair equally, no leverage." },
};

const frameName = (minutes: string) => (minutes === "15" ? "15 min" : `${Number(minutes) / 60} h`);

/** Human name and one-line explanation for any strategy id (TA, placebo control, trend track). */
export function strategyLabel(id: string): StrategyInfo {
  if (typeof id !== "string" || !id) return { name: "Unknown strategy", blurb: "" };
  if (id.startsWith("placebo-v1:")) {
    const base = strategyLabel(id.slice("placebo-v1:".length));
    return { name: `Random baseline for ${base.name}`, blurb: "Enters at random times as often as the real strategy: what luck alone would do." };
  }
  if (Object.prototype.hasOwnProperty.call(TREND, id)) return TREND[id]!;
  const [name, minutes] = id.split(":");
  if (name && Object.prototype.hasOwnProperty.call(TA, name)) {
    const base = TA[name]!;
    return minutes && /^\d+$/.test(minutes)
      ? { name: `${base.name} · ${frameName(minutes)}`, blurb: base.blurb }
      : base;
  }
  return { name: humanizeCode(id), blurb: "" };
}

/** Every strategy id the code emits (for tests and legends). */
export const KNOWN_STRATEGY_IDS = [
  ...Object.keys(TA).flatMap((name) => ["15", "60", "240"].flatMap((m) => [`${name}:${m}`, `placebo-v1:${name}:${m}`])),
  ...Object.keys(TREND),
];

// ---------------------------------------------------------------- outcomes

const OUTCOMES: Record<string, { short: string; tone: "bull" | "bear" | "neutral" | "warn" }> = {
  T: { short: "Target hit", tone: "bull" },
  S: { short: "Stopped out", tone: "bear" },
  E: { short: "Expired at time limit", tone: "neutral" },
  L: { short: "Liquidated", tone: "bear" },
  X: { short: "Unresolved (data problem)", tone: "warn" },
  ambiguous: { short: "Unclear (target and stop in the same candle)", tone: "warn" },
  open: { short: "Still open", tone: "neutral" },
  pending: { short: "Not yet known", tone: "neutral" },
};

export function outcomeLabel(code: string | null | undefined) {
  return code && Object.prototype.hasOwnProperty.call(OUTCOMES, code)
    ? OUTCOMES[code]!
    : { short: code ? humanizeCode(code) : "Not yet known", tone: "neutral" as const };
}

// ---------------------------------------------------------------- glossary

export const GLOSSARY: Record<string, { term: string; plain: string }> = {
  R: { term: "R (risk unit)", plain: "Result measured in the risk taken per trade: +1 R gains what the stop would have lost, −1 R is a full stop-out. Includes fees." },
  ATR: { term: "ATR (volatility)", plain: "Average True Range: the typical size of one candle's move, shown as % of price. Higher means a more volatile market." },
  RSI: { term: "RSI", plain: "Relative Strength Index (0–100). Above 70 is often called overbought, below 30 oversold." },
  EMA: { term: "EMA", plain: "Exponential moving average: a smoothed price. Price above its EMA suggests an uptrend." },
  funding: { term: "Funding", plain: "Payments between long and short traders every few hours on perpetual futures. Included in all results." },
  placebo: { term: "Random baseline", plain: "The same number of trades entered at random times. If a strategy can't beat it, its results are likely luck." },
  scoreBand: { term: "Score band", plain: "Groups trades by the indicator score at entry. Not available until strategies produce scores." },
  score: { term: "Indicator score", plain: "From −100 (all indicators bearish) to +100 (all bullish). It ranks how the indicators look; it is not a win probability or a prediction of profit." },
  turnover: { term: "Turnover", plain: "How much of the portfolio is traded per day. Every unit costs fees (11 bp)." },
  volTarget: { term: "Volatility target", plain: "Positions are sized so the portfolio would swing about this much per year: bigger in calm markets, smaller in wild ones." },
  live: { term: "Live days", plain: "Days whose position was recorded before the day happened, then scored afterwards. This is the honest forward test." },
  backfilled: { term: "Back-filled days", plain: "Days replayed from history after the fact. Useful context, but less trustworthy than live days." },
  mde: { term: "Minimum detectable edge", plain: "The smallest average daily return this many days could reliably detect (power estimate). It is not a significance test; below it, results cannot be told apart from noise." },
  gross: { term: "Gross exposure", plain: "Total position size as a multiple of the portfolio, longs and shorts added. 1× = fully invested." },
  rr: { term: "Reward : risk", plain: "Distance to the profit target divided by the distance to the stop. 1.5 means the target is 1.5× further away than the stop." },
};
