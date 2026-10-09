/** Plain-language reading of the forward job states for the dashboard header. */
export type StatusLine = { level: "ok" | "wait" | "problem"; title: string; detail: string };

type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  { status: string; reason: string | null; through_day_ms?: number | null } | null | undefined;

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  const title = "Signal engine (TA strategies, setups, paper wallet)";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "It has not run yet. Press “Run now”, or leave the local job running.",
    };
  }
  const reason = run.reason ?? "";
  if (/collector missing|no candles/i.test(reason)) {
    return {
      level: "wait",
      title,
      detail:
        "Waiting for market data: the Binance collector has not stored candles for these symbols yet, so no signals can be produced. This is normal right after a database reset or first start; start the app with the collector (npm run dev:local:all) and give it time to fill the history (each timeframe needs up to 260 completed candles).",
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
    detail: `Last run ended “${run.status}”${reason ? `: ${reason}` : ""}.`,
  };
}

export function explainTrendTrack(
  run: TrendRun,
  prospectiveDays: number,
  hasWeights: boolean,
): StatusLine {
  const title = "Daily trend track (hypothetical portfolios)";
  if (!run) {
    return { level: "wait", title, detail: "It has not run yet. Press “Run trend now”." };
  }
  const reason = run.reason ?? "";
  if (/funding/i.test(reason)) {
    return {
      level: "problem",
      title,
      detail: `Recording daily positions${hasWeights ? " (today’s weights are saved)" : ""}, but the funding costs could not be fetched, so days cannot be scored yet. Cause: ${reason}`,
    };
  }
  if (run.status !== "ok") {
    return {
      level: "problem",
      title,
      detail: `Last run ended “${run.status}”${reason ? `: ${reason}` : ""}.`,
    };
  }
  return prospectiveDays === 0
    ? {
        level: "wait",
        title,
        detail:
          "Working. Today’s positions are recorded; the first scored day appears after the next daily close (00:00 UTC). Results stay empty until then.",
      }
    : { level: "ok", title, detail: `Working. ${prospectiveDays} day(s) scored so far.` };
}
