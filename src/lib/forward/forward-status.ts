import { monitoringRunLabel, issueSummary, isStorageIssue } from "@/lib/presentation/labels";
/** Plain-language reading of the forward job states for the dashboard header. */
export type StatusLine = { level: "ok" | "wait" | "problem"; title: string; detail: string };

type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  | { status: string; reason: string | null; through_day_ms?: number | null }
  | null
  | undefined;

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  const title = "Signal engine";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "No evaluation yet. Choose “Evaluate signals now” to check for setups.",
    };
  }
  const reason = run.reason ?? "";
  if (isStorageIssue(reason)) return { level: "problem", title, detail: issueSummary(reason) };
  if (/collector missing|no candles|insufficient_history/i.test(reason)) {
    return {
      level: "wait",
      title,
      detail:
        "Waiting for market history. The collector is still building enough completed-candle history for these strategies.",
    };
  }
  if (run.status === "ok") {
    const age = Math.round((now - run.boundary_ms) / 60_000);
    return age < 120
      ? { level: "ok", title, detail: `Working. Last successful run ${age} min ago.` }
      : {
          level: "problem",
          title,
          detail: `Last successful run was ${Math.round(age / 60)} h ago; the job may have stopped.`,
        };
  }
  return {
    level: "problem",
    title,
    detail: `${monitoringRunLabel(run.status)}. ${reason ? issueSummary(reason) : "Review technical details for this evaluation."}`,
  };
}

export function explainTrendTrack(
  run: TrendRun,
  prospectiveDays: number,
  hasWeights: boolean,
): StatusLine {
  const title = "Daily portfolios";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "No evaluation yet. Choose “Evaluate daily portfolios” to begin.",
    };
  }
  const reason = run.reason ?? "";
  if (isStorageIssue(reason)) return { level: "problem", title, detail: issueSummary(reason) };
  if (/funding/i.test(reason)) {
    return {
      level: "problem",
      title,
      detail: `${hasWeights ? "Portfolio weights are saved, but funding" : "Funding"} costs could not be fetched, so days cannot be scored yet. Review the evaluation’s technical details.`,
    };
  }
  if (run.status !== "ok") {
    return {
      level: "problem",
      title,
      detail: `${monitoringRunLabel(run.status)}. ${reason ? issueSummary(reason) : "Review technical details for this evaluation."}`,
    };
  }
  return prospectiveDays === 0
    ? {
        level: "wait",
        title,
        detail: hasWeights
          ? "Weights are recorded. Waiting for the first scored day after a daily close (00:00 UTC)."
          : "Waiting for portfolio observations. No current weights are available yet.",
      }
    : { level: "ok", title, detail: `Working. ${prospectiveDays} day(s) scored so far.` };
}
