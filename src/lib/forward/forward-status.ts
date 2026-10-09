import { humanizeReason } from "../labels";

/** Display-only reading of canonical job states; never persisted back to the engine. */
export type StatusLine = {
  level: "ok" | "wait" | "problem";
  title: string;
  detail: string;
  action?: string;
  technicalHelp?: string;
};

type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  { status: string; reason: string | null; through_day_ms?: number | null } | null | undefined;

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  const title = "Signals & Paper Trading";
  if (!run)
    return {
      level: "wait",
      title,
      detail: "No signal checks have run yet.",
      action: "Select “Check for new signals now” to start, or keep the scheduled job running.",
    };
  const reason = run.reason ?? "";
  // Keep missing-collector/candle precedence over the run status.
  if (/collector missing|no candles/i.test(reason))
    return {
      level: "wait",
      title,
      detail: "Waiting for market data; the collector has not stored enough price history yet.",
      action: "Start data collection and allow time for history to arrive.",
      technicalHelp:
        "For a local setup, start the app and collector with npm run dev:local:all. Each timeframe needs up to 260 completed candles; this is normal after a first start or database reset.",
    };
  if (run.status === "skipped_stale")
    return {
      level: "wait",
      title,
      detail: humanizeReason(run.status).short,
      action: "Check Data health in Control room, then retry after prices catch up.",
    };
  if (/funding/i.test(reason))
    return {
      level: "problem",
      title,
      detail:
        "Funding rates could not be fetched; affected trade outcomes cannot be finalised yet.",
      action: "Try another signal check when exchange funding data is available.",
    };
  if (run.status === "ok") {
    const age = Math.max(0, Math.round((now - run.boundary_ms) / 60_000));
    return age < 120
      ? {
          level: "ok",
          title,
          detail: `Working. Last successful check ${age} min ago.`,
          action: "Results appear after a signal fires and its trade closes.",
        }
      : {
          level: "problem",
          title,
          detail: `The last successful check was ${Math.round(age / 60)} hours ago; the job may have stopped.`,
          action: "Run a signal check and review collection health if it cannot complete.",
        };
  }
  const label = humanizeReason(reason || run.status);
  return {
    level: label.level ?? "problem",
    title,
    detail: label.short,
    action: label.help ?? "Review the technical details, then try another signal check.",
  };
}

export function explainTrendTrack(
  run: TrendRun,
  prospectiveDays: number,
  hasWeights: boolean,
): StatusLine {
  const title = "Daily Trend Portfolios";
  if (!run)
    return {
      level: "wait",
      title,
      detail: "No daily portfolio updates have run yet.",
      action: "Select “Update trend portfolios now” to record the first positions.",
    };
  const reason = run.reason ?? "";
  // Funding failures take precedence, including on otherwise successful runs.
  if (/funding/i.test(reason))
    return {
      level: "problem",
      title,
      detail: humanizeReason("funding_unavailable").short,
      action: `${hasWeights ? "Today's weights are saved. " : ""}Retry when exchange funding data is available.`,
    };
  if (run.status !== "ok") {
    const label = humanizeReason(reason || run.status);
    return {
      level: label.level ?? "problem",
      title,
      detail: label.short,
      action: label.help ?? "Review the technical details, then update the portfolios again.",
    };
  }
  return prospectiveDays === 0
    ? {
        level: "wait",
        title,
        detail: "Working. Waiting for the first scored day.",
        action:
          "The first result appears after the next daily close (00:00 UTC) once the portfolio update completes.",
      }
    : {
        level: "ok",
        title,
        detail: `Working. ${prospectiveDays} live day${prospectiveDays === 1 ? "" : "s"} scored so far.`,
        action: "Keep the daily job running to build a longer sample.",
      };
}
