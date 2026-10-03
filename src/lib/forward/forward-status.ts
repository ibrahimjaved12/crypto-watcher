import { issueSummary, monitoringRunLabel } from "@/lib/presentation/labels";

/** Product copy only; raw run status and reason remain available in technical details. */
export type StatusLine = { level: "ok" | "wait" | "problem"; title: string; detail: string };
type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  { status: string; reason: string | null; through_day_ms?: number | null } | null | undefined;

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  if (!run)
    return {
      level: "wait",
      title: "Ready for the first evaluation",
      detail: "Evaluate signals to check completed market candles for setups.",
    };
  const reason = run.reason ?? "";
  if (
    run.status === "skipped_stale" &&
    /collector missing|no candles|insufficient.history/i.test(reason)
  )
    return {
      level: "wait",
      title: "Waiting for market history",
      detail:
        "The collector is still building enough completed-candle history for these strategies.",
    };
  if (run.reason)
    return {
      level: "problem",
      title:
        run.status === "ok" ? "Signal evaluation needs attention" : monitoringRunLabel(run.status),
      detail: issueSummary(run.reason),
    };
  if (run.status === "ok") {
    const age = Math.round((now - run.boundary_ms) / 60_000);
    return age < 120
      ? {
          level: "ok",
          title: "Signal evaluation is up to date",
          detail: `Last successful evaluation ${age} min ago. No signal is also a valid result.`,
        }
      : {
          level: "problem",
          title: "Signal evaluation is delayed",
          detail: `Last successful evaluation was ${Math.round(age / 60)} h ago. Evaluate signals to request a fresh check.`,
        };
  }
  return { level: "problem", title: monitoringRunLabel(run.status), detail: issueSummary(reason) };
}

export function explainTrendTrack(
  run: TrendRun,
  prospectiveDays: number,
  hasWeights: boolean,
): StatusLine {
  if (!run)
    return {
      level: "wait",
      title: "Ready for the first portfolio evaluation",
      detail: "Evaluate daily portfolios to begin recording hypothetical results.",
    };
  if (run.reason)
    return {
      level: "problem",
      title: "Portfolio evaluation needs attention",
      detail: issueSummary(run.reason),
    };
  if (run.status !== "ok")
    return {
      level: "problem",
      title: monitoringRunLabel(run.status),
      detail: issueSummary(run.reason),
    };
  return prospectiveDays === 0
    ? {
        level: "wait",
        title: "Awaiting completed daily results",
        detail: hasWeights
          ? "Portfolio weights are recorded. Results appear once a daily close (00:00 UTC) can be evaluated."
          : "No scored daily observations yet. Recorded weights and completed results will appear here when available.",
      }
    : {
        level: "ok",
        title: "Daily results are being recorded",
        detail: `Up to ${prospectiveDays} days scored per track in the before-close sample. This does not establish a trading edge.`,
      };
}
