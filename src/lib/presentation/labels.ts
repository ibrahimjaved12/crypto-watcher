/** Display-only translations. Canonical values remain unchanged in storage and requests. */
export function humanizeCode(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) return "Unavailable";
  const words = value.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  const text = words === words.toUpperCase() ? words.toLowerCase() : words;
  return text.charAt(0).toUpperCase() + text.slice(1);
}
const label = (map: Record<string, string>, value: string | null | undefined) =>
  value
    ? Object.prototype.hasOwnProperty.call(map, value.toLowerCase())
      ? map[value.toLowerCase()]!
      : humanizeCode(value)
    : "Unavailable";

export const sourceLabel = (value: string | null | undefined) =>
  label(
    {
      "binance-usdm": "Binance USD-M Futures",
      "kraken-futures": "Kraken Futures",
      "okx-usdt-swap": "OKX USDT Swap",
      binance: "Binance",
      bybit: "Bybit",
      okx: "OKX",
    },
    value,
  );
export const monitoringRunLabel = (value: string | null | undefined) =>
  label(
    {
      success: "Completed",
      ok: "Completed",
      partial: "Completed with issues",
      failed: "Failed",
      skipped_stale: "Skipped — market data was stale",
      skipped: "Skipped",
      running: "In progress",
      almost_done: "Finishing up",
    },
    value,
  );
export const collectorStatusLabel = (value: string | null | undefined) =>
  label(
    {
      live: "Live",
      stale: "Delayed",
      recovering: "Recovering",
      unavailable: "Unavailable",
      fresh: "Fresh",
      delayed: "Delayed",
    },
    value,
  );
export const freshnessLabel = collectorStatusLabel;
export const outcomeLabel = (value: string | null | undefined) =>
  label({ pending: "Awaiting result" }, value);
export const sampleLabel = (value: string) =>
  label(
    {
      prospective: "Recorded before daily close",
      retrospective: "Reconstructed after the close",
    },
    value,
  );
export const pairLabel = (value: string) =>
  value.endsWith("USDT") && !value.includes("/") ? `${value.slice(0, -4)}/USDT` : value;

const strategies: Record<string, string> = {
  ema_cross_20_50: "EMA 20/50 Crossover",
  ema_cross_50_200: "EMA 50/200 Crossover",
  rsi_14_reversion: "RSI 14 Mean Reversion",
  macd_12_26_9: "MACD 12/26/9",
  bollinger_20_2_reversion: "Bollinger 20/2 Mean Reversion",
  keltner_breakout_20_14_2: "Keltner 20/14/2 Breakout",
};
export function strategyLabel(value: string): string {
  return value.startsWith("placebo-v1:")
    ? `Control · ${strategyLabel(value.slice("placebo-v1:".length))}`
    : label(strategies, value);
}
export const trendVariantLabel = (value: string) =>
  label(
    {
      ens_ls_25: "Donchian Ensemble · Long/Short · 25% vol target",
      ens_lo_25: "Donchian Ensemble · Long-only · 25% vol target",
      ens_ls_15: "Donchian Ensemble · Long/Short · 15% vol target",
      ens_ls_25_sub3: "Donchian Ensemble · 3 lookbacks · 25% vol target",
      tsmom_7: "7-day Time-Series Momentum",
      tsmom_14: "14-day Time-Series Momentum",
      tsmom_28: "28-day Time-Series Momentum",
      ens_ls_25_mfilter: "Donchian Ensemble · MA filter · 25% vol target",
      ens_ls_25_rvfilter: "Donchian Ensemble · Volatility filter · 25% vol target",
    },
    value,
  );
export const benchmarkLabel = (value: string) =>
  label(
    {
      bh_vt_std90_25: "Vol-targeted Buy & Hold · 90-day volatility · 25% target",
      bh_vt_std90_15: "Vol-targeted Buy & Hold · 90-day volatility · 15% target",
      bh_vt_ewma60_25: "Vol-targeted Buy & Hold · EWMA-60 · 25% target",
      ew_long: "Equal-weight Long Benchmark",
    },
    value,
  );

export const isStorageIssue = (value: string) =>
  /constraint|could not.*sav|failed.*sav|insert.*fail|database|\bDB\b/i.test(value);

/** Known diagnostic messages summarized without concealing persistence failures. */
export function issueSummary(value: string): string {
  if (isStorageIssue(value))
    return /\bTA\b|technical.analysis|ta_signals/i.test(value)
      ? "A technical-analysis record could not be saved."
      : "A data storage operation failed. Review technical details.";
  if (/catch-up gap|catchup gap/i.test(value))
    return "Technical-analysis history is too far behind to catch up from retained candles.";
  if (/insufficient_history|collector missing|no candles/i.test(value))
    return "Not enough completed candle history is available yet.";
  if (/funding/i.test(value))
    return "Funding data is unavailable. Portfolio results cannot be completed yet.";
  if (/stale/i.test(value))
    return "Market data is delayed. This check could not use fresh candles.";
  return "The operation could not complete normally. Review technical details.";
}

export function alertRuleLabel(alert: {
  change_pct: number | string;
  threshold_pct: number | string;
  comparison_mode?: string | null;
  window_minutes: number | null;
}): string {
  const direction = Number(alert.change_pct) < 0 ? "Dropped" : "Rose";
  const comparison =
    alert.comparison_mode === "baseline"
      ? "from saved baseline"
      : alert.window_minutes == null
        ? "over the comparison window"
        : `over ${alert.window_minutes} minutes`;
  return `${direction} ${alert.threshold_pct}% ${comparison}`;
}
