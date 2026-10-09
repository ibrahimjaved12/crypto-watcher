/** Display language only. Machine values remain canonical in storage and API contracts. */
export type ReasonLabel = { short: string; help?: string; level?: "ok" | "wait" | "problem" };

const reasons: Record<string, ReasonLabel> = {
  ok: { short: "Ready", level: "ok" },
  success: { short: "Completed", level: "ok" },
  failed: {
    short: "Could not complete",
    help: "Review the details, then try again.",
    level: "problem",
  },
  partial: {
    short: "Partly completed",
    help: "Some data was unavailable. Review the details before using these results.",
    level: "wait",
  },
  skipped: {
    short: "Not run",
    help: "The check was skipped; review collection settings and the run details.",
    level: "wait",
  },
  skipped_stale: {
    short:
      "Skipped: market data was too old to trust, so no trades were simulated (safe behaviour)",
    help: "Wait for fresh market data, then check again.",
    level: "wait",
  },
  funding_unavailable: {
    short: "Funding rates could not be fetched, so days cannot be scored yet",
    help: "Try again when exchange funding data is available.",
    level: "problem",
  },
  "collector missing": {
    short: "Waiting for the market-data collector",
    help: "Start the collector and allow time for price history to arrive.",
    level: "wait",
  },
  "no candles": {
    short: "No price history collected yet",
    help: "Keep data collection running; results appear once enough candles arrive.",
    level: "wait",
  },
  provider_timeout: {
    short: "The exchange took too long to respond",
    help: "Try again shortly.",
    level: "problem",
  },
  provider_unavailable: {
    short: "The exchange is unavailable",
    help: "Try again shortly.",
    level: "problem",
  },
  invalid_or_stale_data: {
    short: "Market data is incomplete or too old to use",
    help: "Wait for a fresh, complete update.",
    level: "wait",
  },
  invalid_data: {
    short: "Market data did not pass the quality checks",
    help: "Wait for the next exchange update.",
    level: "problem",
  },
  stale_data: {
    short: "Market data is too old to use",
    help: "Check collection health and wait for fresh prices.",
    level: "wait",
  },
  stale: { short: "Waiting for fresh data", level: "wait" },
  insufficient_history: {
    short: "Not enough past candles yet for this timeframe",
    help: "Keep collection running while the price history fills up.",
    level: "wait",
  },
  insufficient: { short: "Not enough price history yet", level: "wait" },
  unavailable: {
    short: "Data unavailable",
    help: "Try again after the next market update.",
    level: "wait",
  },
  already_done: { short: "Already up to date", level: "ok" },
  pending: { short: "Not yet known", level: "wait" },
  open: { short: "Still open", level: "wait" },
  live: { short: "Live", level: "ok" },
  recovering: { short: "Lagging", level: "wait" },
  delayed: { short: "Lagging", level: "wait" },
  offline: { short: "Offline", level: "problem" },
  disabled: { short: "Paused", level: "wait" },
  invalid_state: { short: "The saved reference needs to be repaired", level: "problem" },
  baseline_required: { short: "Waiting for the first reference price", level: "wait" },
  baseline_reset_required: { short: "A new reference price is needed", level: "wait" },
  already_processed: { short: "This price update was already checked", level: "ok" },
  below_threshold: { short: "Price movement is below your threshold", level: "ok" },
  cooldown: { short: "Waiting before sending another alert", level: "wait" },
  eligible: { short: "Ready for an alert at this snapshot", level: "ok" },
  bullish: { short: "Bullish lean" },
  bearish: { short: "Bearish lean" },
  neutral: { short: "No clear lean" },
};

const patterns: [RegExp, ReasonLabel | string][] = [
  [
    /TA catch-up gap exceeds .*history/i,
    {
      short:
        "Technical analysis could not catch up: the gap since the last run is longer than the stored history.",
      help: "It will recover as new candles arrive.",
      level: "wait",
    },
  ],
  [
    /ta_signals_futures_provenance_check/i,
    {
      short:
        "A saved signal was rejected because its data source details were incomplete (internal consistency check)",
      help: "Review the original details if this keeps happening.",
      level: "problem",
    },
  ],
  [/collector missing/i, "collector missing"],
  [/no candles/i, "no candles"],
  [/funding[_ ]unavailable|funding.*(?:failed|unavailable|fetch)/i, "funding_unavailable"],
  [/skipped_stale/i, "skipped_stale"],
  [/insufficient_history|insufficient history|not enough.*candles/i, "insufficient_history"],
  [/provider_timeout|timed? out|timeout/i, "provider_timeout"],
  [/provider_unavailable/i, "provider_unavailable"],
  [/invalid_or_stale_data/i, "invalid_or_stale_data"],
  [/invalid_data/i, "invalid_data"],
  [/stale_data/i, "stale_data"],
  [
    /incomplete startup/i,
    {
      short: "Waiting for the full portfolio price history",
      help: "Keep collection running, then update the portfolios again.",
      level: "wait",
    },
  ],
];

function sentence(raw: string): string {
  const words = raw
    .trim()
    .replace(/[_:]+/g, " ")
    .replace(/\bpython\b/gi, "analysis service")
    .replace(/\s+/g, " ");
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : "No details available";
}

export function humanizeReason(raw: string | null | undefined): ReasonLabel {
  if (typeof raw !== "string" || !raw.trim()) return { short: "No details available" };
  const key = raw.trim().toLowerCase();
  if (Object.hasOwn(reasons, key)) return { ...reasons[key]! };
  for (const [pattern, label] of patterns) {
    if (pattern.test(raw)) return { ...(typeof label === "string" ? reasons[label]! : label) };
  }
  return { short: sentence(raw) };
}

const strategies: Record<string, { name: string; blurb: string }> = {
  ema_cross_20_50: {
    name: "Moving-average crossover · 20 / 50",
    blurb: "Follows a crossing of the 20- and 50-candle exponential moving averages.",
  },
  ema_cross_50_200: {
    name: "Moving-average crossover · 50 / 200",
    blurb: "Follows a crossing of the 50- and 200-candle exponential moving averages.",
  },
  rsi_14_reversion: {
    name: "Momentum reversal · 14 candles",
    blurb: "Tests reversals as the relative strength index returns from an extreme.",
  },
  macd_12_26_9: {
    name: "Momentum crossover · 12 / 26 / 9",
    blurb: "Tests crossings of the moving-average convergence/divergence signal line.",
  },
  bollinger_20_2_reversion: {
    name: "Bollinger band reversal",
    blurb: "Tests returns inside a 20-candle band set two standard deviations from its average.",
  },
  keltner_breakout_20_14_2: {
    name: "Keltner channel breakout",
    blurb: "Follows breaks outside a 20-candle average channel sized by recent price movement.",
  },
  ens_ls_25: {
    name: "Trend ensemble · long/short · 25% target",
    blurb:
      "Combines trend lookbacks, allowing long and short positions with a 25% annual volatility target.",
  },
  ens_lo_25: {
    name: "Trend ensemble · long only · 25% target",
    blurb:
      "Combines trend lookbacks, taking only long positions with a 25% annual volatility target.",
  },
  ens_ls_15: {
    name: "Trend ensemble · long/short · 15% target",
    blurb: "Combines trend lookbacks with a lower 15% annual volatility target.",
  },
  ens_ls_25_sub3: {
    name: "Three-lookback trend ensemble",
    blurb: "Tests a smaller set of three trend lookbacks with a 25% annual volatility target.",
  },
  ens_ls_25_mfilter: {
    name: "Trend ensemble · average filter",
    blurb: "Filters trend entries using moving-average agreement; targets 25% annual volatility.",
  },
  ens_ls_25_rvfilter: {
    name: "Trend ensemble · volatility filter",
    blurb: "Filters trend entries during high realized volatility; targets 25% annual volatility.",
  },
  ew_long: {
    name: "Equal-weight buy and hold",
    blurb: "A comparison portfolio that holds each active coin with equal weight, long only.",
  },
};

export function strategyLabel(id: string): { name: string; blurb: string } {
  if (id.startsWith("placebo-v1:")) {
    const matched = strategyLabel(id.slice("placebo-v1:".length));
    return {
      name: `Random entries · ${matched.name}`,
      blurb: `Random entry directions matched to the signal frequency of ${matched.name.toLowerCase()}.`,
    };
  }
  const frame = /^(.*):(15|60|240)$/.exec(id);
  if (frame) {
    const base = strategyLabel(frame[1]!);
    const minutes = Number(frame[2]);
    return {
      name: `${base.name} · ${minutes < 60 ? `${minutes}m` : `${minutes / 60}h`}`,
      blurb: base.blurb,
    };
  }
  if (Object.hasOwn(strategies, id)) return { ...strategies[id]! };
  const momentum = /^tsmom_(7|14|28)$/.exec(id);
  if (momentum)
    return {
      name: `${momentum[1]}-day momentum`,
      blurb: `Follows the direction of the past ${momentum[1]} days of returns, targeting 25% annual volatility.`,
    };
  const hold = /^bh_vt_(std90|ewma60)_(15|25)$/.exec(id);
  if (hold)
    return {
      name: `Buy and hold · ${hold[2]}% volatility target · ${hold[1] === "std90" ? "90-day sizing" : "weighted sizing"}`,
      blurb: `Long-only comparison using ${hold[1] === "std90" ? "90-day volatility" : "volatility weighted toward recent days"} to adjust exposure. The target is volatility, not a promised return.`,
    };
  return {
    name: sentence(id),
    blurb: "An unrecognised strategy; review its technical details before interpreting results.",
  };
}

const outcomes: Record<string, string> = {
  T: "Target hit",
  S: "Stopped out",
  E: "Expired at time limit",
  L: "Liquidated",
  X: "Unresolved",
  ambiguous: "Unclear (target and stop in the same candle)",
  open: "Still open",
};
export function outcomeLabel(code: string | null | undefined): string {
  return code && Object.hasOwn(outcomes, code) ? outcomes[code]! : humanizeReason(code).short;
}

export const GLOSSARY: Record<string, { term: string; plain: string }> = {
  R: {
    term: "R",
    plain:
      "R = risk taken per trade. A result of +1 R earns that amount; −1 R loses it. Results shown are after costs.",
  },
  ATR: {
    term: "Volatility (ATR %)",
    plain:
      "Average true range measures the typical candle movement, including gaps. As a percentage of price, it measures size of movement, not direction.",
  },
  RSI: {
    term: "Momentum (RSI 14)",
    plain:
      "Relative strength index over 14 candles: below 30 is oversold, above 70 is overbought. Neither guarantees a reversal.",
  },
  EMA: {
    term: "Moving averages (EMA)",
    plain:
      "Exponential moving averages give recent prices more weight. 20, 50 and 200 are candle counts, not days.",
  },
  funding: {
    term: "Funding",
    plain:
      "Periodic payments between long and short perpetual futures positions. They can add to or subtract from a simulated result.",
  },
  placebo: {
    term: "Random-entry comparison",
    plain:
      "What random entries with a matched signal frequency would have done, after the same costs. A useful comparison, not proof of an edge.",
  },
  baseline: {
    term: "Reference price",
    plain: "The last saved price used to measure cumulative movement for your alert rule.",
  },
  "score band": {
    term: "Score band",
    plain:
      "A grouping of indicator scores for comparing outcomes. No score band is assigned to these strategy results yet.",
  },
  "indicator score": {
    term: "Indicator score",
    plain:
      "−100 is the strongest bearish lean; +100 the strongest bullish lean. This ranks the indicators, not the probability of a winning trade or future profit.",
  },
  turnover: {
    term: "Turnover / day",
    plain:
      "How much portfolio exposure changes each day. Higher turnover means more trading and more fees.",
  },
  "vol-target": {
    term: "Volatility target",
    plain:
      "Adjusts position sizes to aim for a chosen annual volatility. It is a risk target, not a return forecast or guarantee.",
  },
  prospective: {
    term: "Live days",
    plain:
      "Scored after they happened, with the decision recorded before the daily close. It may have been recorded after the open; returns still use the full open-to-close day.",
  },
  reconstructed: {
    term: "Back-filled days",
    plain:
      "Replayed from history after the daily close. Less trustworthy as evidence than decisions recorded while the test was running.",
  },
  "minimum detectable edge": {
    term: "Minimum detectable edge",
    plain:
      "The smallest average daily advantage this sample could reliably distinguish from noise under the study assumptions. Short samples can only detect large effects.",
  },
  "reward/risk": {
    term: "Reward / risk",
    plain:
      "The target gain divided by the risk to the stop, before costs. For example 3/2 means a target of 1.5 times the risk.",
  },
  gross: {
    term: "Gross exposure",
    plain:
      "Total absolute portfolio exposure, counting both long and short positions. 1× means exposure equal to the portfolio value.",
  },
};
