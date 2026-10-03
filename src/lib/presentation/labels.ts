/** Display names only. Canonical codes and all scientific calculations stay unchanged. */
export function humanizeCode(value: string | null | undefined): string {
  if (!value?.trim()) return "Unavailable";
  const text = value.trim().replace(/[_-]+/g, " ").replace(/\s+/g, " ").toLowerCase();
  return (text.charAt(0).toUpperCase() + text.slice(1)).replace(
    /\b(ema|rsi|macd|atr|adx|utc|usd|usdt|btc|eth|sol|ma)\b/gi,
    (acronym) => acronym.toUpperCase(),
  );
}

const label = (names: Record<string, string>, value: string | null | undefined) =>
  value ? (names[value.toLowerCase()] ?? humanizeCode(value)) : "Unavailable";

export const sourceLabel = (value: string | null | undefined) =>
  label(
    {
      "binance-usdm": "Binance USD-M Futures",
      binance: "Binance",
      bybit: "Bybit",
      "kraken-futures": "Kraken Futures",
      "okx-usdt-swap": "OKX USDT Swap",
      test: "Test data",
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
      skipped: "Skipped",
      skipped_stale: "Skipped — market data was stale",
      already_done: "Already evaluated",
      almost_done: "Almost complete",
      running: "In progress",
    },
    value,
  );

export const collectorStatusLabel = (value: string | null | undefined) =>
  label(
    {
      live: "Live",
      stale: "Delayed",
      delayed: "Delayed",
      recovering: "Recovering",
      unavailable: "Unavailable",
      fresh: "Fresh",
      warming_up: "Waiting for history",
    },
    value,
  );
export const freshnessLabel = collectorStatusLabel;
export const outcomeLabel = (value: string | null | undefined) =>
  label(
    {
      pending: "Awaiting result",
      t: "Target reached",
      s: "Stop reached",
      ambiguous: "Ambiguous outcome",
      expired: "Evaluation window ended",
    },
    value,
  );

const strategies: Record<string, string> = {
  ema_cross_20_50: "EMA 20/50 Crossover",
  ema_cross_50_200: "EMA 50/200 Crossover",
  rsi_14_reversion: "RSI 14 Mean Reversion",
  macd_12_26_9: "MACD 12/26/9",
  bollinger_20_2_reversion: "Bollinger 20/2 Mean Reversion",
  keltner_breakout_20_14_2: "Keltner 20/14/2 Breakout",
};
export function strategyLabel(value: string): string {
  if (value.startsWith("placebo-v1:")) return `Control · ${strategyLabel(value.slice(11))}`;
  // Persisted signal IDs include the candle timeframe, e.g. ema_cross_20_50:60.
  const timed = value.match(/^(.+):(\d+)$/);
  if (timed) {
    const minutes = Number(timed[2]);
    const timeframe = minutes >= 60 ? `${minutes / 60}h` : `${minutes}m`;
    return `${label(strategies, timed[1])} · ${timeframe}`;
  }
  return label(strategies, value);
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
export const sampleLabel = (value: string) =>
  label(
    {
      prospective: "Recorded before daily close",
      retrospective: "Reconstructed after the close",
    },
    value,
  );
export const pairLabel = (value: string) => value.replace(/^([A-Z0-9]+)USDT$/, "$1/USDT");

/** Summarize known operational failures without printing diagnostic prose in product UI.
 * Save failures take precedence over history gaps in concatenated errors.
 * The caller must keep the original error available in technical details.
 */
export function issueSummary(raw: string | null | undefined): string {
  if (!raw) return "Details are unavailable. Try again or review system health in Settings.";
  if (/constraint|ta_signals.*(?:insert|save)|(?:save|insert).*ta_signals/i.test(raw))
    return "A technical-analysis record could not be saved.";
  if (/database|databaseWrite|permission denied|row.level security/i.test(raw))
    return "Some data could not be read or saved. Review the technical details.";
  if (/catch.up gap exceeds/i.test(raw))
    return "Technical-analysis history is too far behind to catch up from retained candles.";
  if (/funding/i.test(raw))
    return "Funding costs are unavailable. Some results could not be evaluated.";
  if (/insufficient[_ ]history|no[_ ]completed[_ ]candles|no candles|collector missing/i.test(raw))
    return "Not enough completed candle history is available for this timeframe yet.";
  if (/missing_candles|gap\(s\) in candle history|last completed minute missing/i.test(raw))
    return "Completed candles are missing. Analysis is waiting for a continuous market history.";
  if (/invalid.*data|mismatch|invalid_state/i.test(raw))
    return "Market data or saved state could not be verified. Analysis is unavailable.";
  if (/stale|delayed/i.test(raw))
    return "Market data is delayed. Fresh completed candles are needed.";
  if (/collector recovering/i.test(raw))
    return "Market collection is recovering. Fresh completed candles are needed.";
  if (/not every track is finalised/i.test(raw))
    return "Some daily portfolios are still awaiting complete results.";
  if (/timeout|timed out/i.test(raw))
    return "The data source took too long to respond. Try again shortly.";
  if (/provider_unavailable|unavailable|fetch failed|network/i.test(raw))
    return "The data source is unavailable. Try again shortly.";
  return "The operation encountered an issue. Review the technical details.";
}

export type StatusTone = "good" | "warning" | "danger" | "neutral";
export function statusTone(value: string | null | undefined): StatusTone {
  if (/^(live|fresh|success|ok|completed|bullish)$/i.test(value ?? "")) return "good";
  if (/^(failed|unavailable|error|bearish)$/i.test(value ?? "")) return "danger";
  if (/^(partial|stale|delayed|recovering|skipped_stale|insufficient|pending)$/i.test(value ?? ""))
    return "warning";
  return "neutral";
}

export function alertRuleLabel(alert: {
  change_pct: number;
  threshold_pct: number;
  comparison_mode: string;
  window_minutes: number | null;
}) {
  const movement =
    Number(alert.change_pct) < 0 ? "Dropped" : Number(alert.change_pct) > 0 ? "Rose" : "Moved";
  const comparison =
    alert.comparison_mode === "baseline"
      ? "from saved baseline"
      : alert.window_minutes != null
        ? `over ${alert.window_minutes} minutes`
        : "from the reference price";
  return `${movement} at least ${alert.threshold_pct}% ${comparison}`;
}
