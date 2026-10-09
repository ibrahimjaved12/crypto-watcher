import { humanizeReason } from "@/lib/labels";
import { isStorageIssue } from "@/lib/presentation/labels";
/**
 * Plain-language reading of the forward job states for the Strategy Lab status cards. `detail` is
 * one plain sentence; `action` says what (if anything) to do; `raw` keeps the machine status and
 * reason for the collapsed Technical details. Machine values are never changed, only translated.
 */
export type StatusLine = {
  level: "ok" | "wait" | "problem";
  title: string;
  detail: string;
  action?: string | undefined;
  raw?: string | undefined;
};

type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  | { status: string; reason: string | null; through_day_ms?: number | null }
  | null
  | undefined;

const rawOf = (run: { status: string; reason: string | null }) =>
  run.reason ? `${run.status} · ${run.reason}` : run.status;

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  const title = "Signals & paper trading";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "No check has run yet.",
      action: "Press “Check for new signals now”, or wait for the hourly job.",
    };
  }
  const reason = run.reason ?? "";
  const raw = rawOf(run);
  if (isStorageIssue(reason)) {
    return { level: "problem", title, detail: humanizeReason(reason).short + ".", raw,
      action: "Open Technical details and check the database connection." };
  }
  if (/collector missing|no candles|insufficient_history|last completed minute missing|gap\(s\) in candle history/i.test(reason)) {
    return {
      level: "wait",
      title,
      detail: "Waiting for market data: the collector has not stored enough recent candles yet.",
      action: "Make sure the market-data collector is running; it backfills by itself.",
      raw,
    };
  }
  if (run.status === "skipped_stale") {
    return {
      level: "wait",
      title,
      detail: "Skipped: market data was too old to trust, so no trades were simulated (safe behaviour).",
      action: "Nothing, unless this repeats for hours. Then check that the collector is live.",
      raw,
    };
  }
  if (run.status === "ok") {
    const age = Math.round((now - run.boundary_ms) / 60_000);
    if (/funding_unavailable/i.test(reason)) {
      return { level: "wait", title, raw,
        detail: "Running, but funding rates could not be fetched, so trades that cross a funding time stay open.",
        action: "Nothing; the next hourly check retries." };
    }
    return age < 120
      ? { level: "ok", title, detail: `Working. Last check ${age} min ago.`, raw }
      : {
          level: "problem",
          title,
          detail: `The last successful check was ${Math.round(age / 60)} h ago; the hourly job may have stopped.`,
          action: "Restart the forward job, or press “Check for new signals now”.",
          raw,
        };
  }
  const info = humanizeReason(reason || run.status);
  return { level: info.level ?? "problem", title, detail: `${info.short}.`, action: info.help, raw };
}

export function explainTrendTrack(
  run: TrendRun,
  prospectiveDays: number,
  hasWeights: boolean,
): StatusLine {
  const title = "Daily trend portfolios";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "Not started yet.",
      action: "Press “Update trend portfolios now”, or wait for the daily job (00:05 UTC).",
    };
  }
  const reason = run.reason ?? "";
  const raw = rawOf(run);
  if (isStorageIssue(reason)) {
    return { level: "problem", title, detail: humanizeReason(reason).short + ".", raw,
      action: "Open Technical details and check the database connection." };
  }
  if (/funding/i.test(reason)) {
    return {
      level: "wait",
      title,
      detail: `${hasWeights ? "Positions are recorded, but funding" : "Funding"} rates could not be fetched, so days cannot be scored yet.`,
      action: "Nothing; the next daily run retries.",
      raw,
    };
  }
  if (run.status !== "ok") {
    const info = humanizeReason(reason || run.status);
    return { level: info.level ?? "problem", title, detail: `${info.short}.`, action: info.help, raw };
  }
  return prospectiveDays === 0
    ? {
        level: "wait",
        title,
        detail: hasWeights
          ? "Positions are recorded. The first live day is scored after the next daily close (00:00 UTC)."
          : "Waiting for the first recorded positions.",
        raw,
      }
    : { level: "ok", title, detail: `Working. ${prospectiveDays} live day(s) scored so far.`, raw };
}
