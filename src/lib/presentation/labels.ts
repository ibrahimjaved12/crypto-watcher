/** Display-only translations. Canonical values remain unchanged in storage and requests. */
import {
  humanizeCode,
  humanizeReason,
  outcomeLabel as outcomeInfo,
  strategyLabel as strategyInfo,
} from "@/lib/labels";

export { humanizeCode, humanizeReason };
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
  humanizeReason(value).short;
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
export const outcomeLabel = (value: string | null | undefined) => outcomeInfo(value).short;
export const sampleLabel = (value: string) =>
  label(
    {
      prospective: "Live days",
      retrospective: "Back-filled days",
    },
    value,
  );
export const pairLabel = (value: string) =>
  value.endsWith("USDT") && !value.includes("/") ? `${value.slice(0, -4)}/USDT` : value;

export const strategyLabel = (value: string): string => strategyInfo(value).name;
export const strategyBlurb = (value: string): string => strategyInfo(value).blurb;
export const trendVariantLabel = strategyLabel;
export const benchmarkLabel = strategyLabel;

export const isStorageIssue = (value: string) =>
  /constraint|could not.*sav|failed.*sav|insert.*fail|database|\bDB\b/i.test(value);

/** Known diagnostic messages summarized without concealing persistence failures. */
export function issueSummary(value: string): string {
  return `${humanizeReason(value).short}.`;
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
