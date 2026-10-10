import { summarizeReasons } from "../labels";

/**
 * Plain-language reading of the forward job states for the Strategy Lab status cards.
 * `detail` is one short sentence; `action` says what to do; `help` holds the longer explanation
 * and `raw` the untouched machine string (shown only under "Technical details").
 */
export type StatusLine = {
  level: "ok" | "wait" | "problem";
  title: string;
  detail: string;
  action?: string | undefined;
  help?: string | undefined;
  raw?: string | undefined;
};

type SignalRun = { status: string; reason: string | null; boundary_ms: number } | null | undefined;
type TrendRun =
  { status: string; reason: string | null; through_day_ms?: number | null } | null | undefined;

const rawOf = (run: { status: string; reason: string | null }) =>
  `${run.status}${run.reason ? ` · ${run.reason}` : ""}`;

const COLLECTOR_HELP =
  "The Binance collector stores one-minute candles for the six coins. Right after a database reset or a first start it has not stored them yet. Locally, start the app together with the collector (npm run dev:local:all) and give it time: each timeframe needs up to 260 completed candles of history.";

export function explainSignalEngine(run: SignalRun, now: number): StatusLine {
  const title = "Signals & paper trading";
  if (!run) {
    return {
      level: "wait",
      title,
      detail: "It has not run yet.",
      action: "Press “Check for new signals now”, or leave the hourly job running.",
    };
  }
  const reason = run.reason ?? "";
  const summary = summarizeReasons(reason);
  if (/collector missing|no candles/i.test(reason)) {
    return {
      level: "wait",
      title,
      detail: "Waiting for market data: no candles are stored yet for some coins, so no signals can be produced.",
      action: `Make sure the live data collector is running, then wait. Affected: ${summary.items.join("; ")}.`,
      help: COLLECTOR_HELP,
      raw: rawOf(run),
    };
  }
  if (run.status === "skipped_stale") {
    return {
      level: "wait",
      title,
      detail:
        "Skipped: market data was too old to trust, so no trades were simulated (safe behaviour).",
      action: summary.items.length
        ? `Why: ${summary.items.join("; ")}. It retries automatically every hour.`
        : "It retries automatically every hour.",
      help: summary.help,
      raw: rawOf(run),
    };
  }
  if (run.status === "ok") {
    const age = Math.round((now - run.boundary_ms) / 60_000);
    const funding = /funding/i.test(reason);
    if (age >= 120) {
      return {
        level: "problem",
        title,
        detail: `Last successful run was ${Math.round(age / 60)} h ago; the job may have stopped.`,
        action: "Check that the hourly job is running, or press “Check for new signals now”.",
        raw: rawOf(run),
      };
    }
    return funding
      ? {
          level: "wait",
          title,
          detail: `Working (last run ${age} min ago), but funding rates could not be fetched, so some trades stay open until they can be scored.`,
          action: "Nothing to do; it retries on the next run.",
          help: summary.help,
          raw: rawOf(run),
        }
      : { level: "ok", title, detail: `Working. Last successful run ${age} min ago.` };
  }
  return {
    level: "problem",
    title,
    detail: `The last run did not finish: ${summary.short}.`,
    action: "Try “Check for new signals now”. If it keeps failing, open Technical details.",
    help: summary.help,
    raw: rawOf(run),
  };
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
      detail: "It has not run yet.",
      action: "Press “Update trend portfolios now”.",
    };
  }
  const reason = run.reason ?? "";
  const summary = summarizeReasons(reason);
  if (/funding/i.test(reason)) {
    return {
      level: "problem",
      title,
      detail: `Recording daily positions${hasWeights ? " (today’s weights are saved)" : ""}, but funding rates could not be fetched, so days cannot be scored yet.`,
      action: "Nothing to do now; the next run retries. If it persists for days, check Binance access.",
      help: summary.items.join("; "),
      raw: rawOf(run),
    };
  }
  if (run.status !== "ok") {
    return {
      level: "problem",
      title,
      detail: `The last run did not finish: ${summary.short}.`,
      action: "Try “Update trend portfolios now”. If it keeps failing, open Technical details.",
      help: summary.items.length > 1 ? summary.items.join("; ") : summary.help,
      raw: rawOf(run),
    };
  }
  return prospectiveDays === 0
    ? {
        level: "wait",
        title,
        detail:
          "Working. Today’s positions are recorded; the first scored day appears after the next daily close (00:00 UTC).",
        action: "Nothing to do; results stay empty until then.",
      }
    : { level: "ok", title, detail: `Working. ${prospectiveDays} live day(s) scored so far.` };
}
