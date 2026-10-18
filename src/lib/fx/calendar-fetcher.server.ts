// Economic calendar fetcher (#401 part 2b). Downloads the Forex Factory weekly feeds, has the Python
// analysis service parse them strictly (parse_ff_week), and appends the observations and one fetch-log
// row per attempt to the local operational database. The news-blackout coverage check
// (calendar_coverage) fails closed when the latest ok fetch is stale or does not reach far enough, so
// this loop keeps it fresh. Pure orchestration: network, parser, store, clock and sleep are injected.
// Every failure is caught and retried; the loop never ends on its own.
import type { FxCalendarObservation, OperationalStore } from "../operational/types";

// ---- Schedule and feed constants: every value here is an UNVERIFIED placeholder. ----
/** UNVERIFIED placeholder: one full cycle (this week, then next week) every 2 hours. */
export const CYCLE_MS = 2 * 3_600_000;
/** UNVERIFIED placeholder: up to 10 minutes of random jitter on the normal cycle. */
export const JITTER_MS = 10 * 60_000;
/** UNVERIFIED placeholder: the owner's observation of the feed file names (no env override). */
export const THIS_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json";
/** UNVERIFIED placeholder: may not exist until late in the week (a 404 is "not published yet"). */
export const NEXT_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json";
/** UNVERIFIED placeholder: identifies this personal tool to the feed host. */
export const USER_AGENT = "crypto-watcher-personal/1";
/** UNVERIFIED placeholder: request timeout and body cap. */
export const FETCH_TIMEOUT_MS = 15_000;
export const MAX_BODY_BYTES = 2 * 1024 * 1024;
/** UNVERIFIED placeholder: first pass after start-up, and the gap between the two sources. */
export const START_DELAY_MS = 5_000;
export const BETWEEN_SOURCES_MS = 30_000;
/** UNVERIFIED placeholder: 429 backoff (Retry-After honoured up to the cap). */
export const RATE_LIMIT_START_MS = 30 * 60_000;
export const RATE_LIMIT_CAP_MS = 6 * 3_600_000;
/** UNVERIFIED placeholder: backoff after any other failure, reset after a success. */
export const ERROR_START_MS = 5 * 60_000;
export const ERROR_CAP_MS = 3_600_000;

export const FX_CALENDAR_SOURCE = "forexfactory";

/** One row as POST /v1/fx/calendar/parse returns it (exactly record_fx_calendar's keys). */
export type ParsedRow = {
  source: string;
  source_event_key: string;
  event_ts: string;
  currency: string;
  impact: "low" | "medium" | "high";
  title: string;
  detail: Record<string, string>;
};

export type ParsedFeed = { rows: ParsedRow[]; coveredToMs: number; skippedHolidays: number; skippedAllLow: number };

/** Thrown by `parse` when the parser rejects the feed (HTTP 422); any other throw means unavailable. */
export class CalendarParseRejected extends Error {}

export type CalendarFetcherDeps = {
  fetchFeed(url: string, signal: AbortSignal): Promise<{ status: number; retryAfterSeconds: number | null; body: string }>;
  /** Calls the Python endpoint. */
  parse(payload: unknown): Promise<ParsedFeed>;
  store: Pick<OperationalStore, "recordFxCalendar" | "recordFxCalendarFetch">;
  now(): number;
  sleep(ms: number, signal: AbortSignal): Promise<void>;
  log(line: string): void;
  /** Jitter source; defaults to Math.random. */
  random?: () => number;
};

export type CalendarSource = {
  label: "this-week" | "next-week";
  url: string;
  /** A 404 is "not published yet" (next week only): no error and no fetch-log row. */
  notPublishedOn404: boolean;
};

export const THIS_WEEK: CalendarSource = { label: "this-week", url: THIS_WEEK_URL, notPublishedOn404: false };
export const NEXT_WEEK: CalendarSource = { label: "next-week", url: NEXT_WEEK_URL, notPublishedOn404: true };

/** Per-source backoff state for one process. */
export type SourceState = { errorStreak: number; rateLimitStreak: number; loggedNotPublished: boolean };
export const newSourceState = (): SourceState => ({ errorStreak: 0, rateLimitStreak: 0, loggedNotPublished: false });

export type PassOutcome = "ok" | "not_published" | "rate_limited" | "error";
export type PassResult = { outcome: PassOutcome; nextDelayMs: number };

/** Fixed error reasons for the fetch log: never a URL, body or header. */
type FailureReason =
  | `http_${number}` | "network" | "too_large" | "bad_json" | "parse_rejected" | "parser_unavailable"
  | "store_unavailable";

const LOG = "[fx-calendar]";
const minutes = (ms: number) => `${Math.round(ms / 60_000)} min`;

function toObservation(row: ParsedRow): FxCalendarObservation {
  const eventTs = Date.parse(row.event_ts);
  if (!Number.isFinite(eventTs)) throw new CalendarParseRejected("event_ts");
  return {
    source: row.source,
    sourceEventKey: row.source_event_key,
    eventTs,
    currency: row.currency,
    impact: row.impact,
    title: row.title,
    detail: row.detail,
  };
}

async function logFailure(deps: CalendarFetcherDeps, error: string): Promise<void> {
  try {
    await deps.store.recordFxCalendarFetch({ source: FX_CALENDAR_SOURCE, status: "error", eventCount: 0, error });
  } catch {
    // The store itself is down: the pass is already an error and is retried later.
  }
}

/** One pass over one source. Never throws (except for an abort of `signal`). */
export async function runPass(
  deps: CalendarFetcherDeps,
  source: CalendarSource,
  state: SourceState = newSourceState(),
  signal: AbortSignal = new AbortController().signal,
): Promise<PassResult> {
  const fail = async (reason: FailureReason): Promise<PassResult> => {
    state.errorStreak += 1;
    state.rateLimitStreak = 0;
    const nextDelayMs = Math.min(ERROR_START_MS * 2 ** (state.errorStreak - 1), ERROR_CAP_MS);
    await logFailure(deps, reason);
    deps.log(`${LOG} ${source.label}: ${reason}; retry in ${minutes(nextDelayMs)}`);
    return { outcome: "error", nextDelayMs };
  };

  let response: Awaited<ReturnType<CalendarFetcherDeps["fetchFeed"]>>;
  try {
    response = await deps.fetchFeed(source.url, AbortSignal.any([signal, AbortSignal.timeout(FETCH_TIMEOUT_MS)]));
  } catch {
    if (signal.aborted) throw signal.reason;
    return fail("network"); // DNS, TLS, reset or the 15 s timeout
  }

  if (response.status === 404 && source.notPublishedOn404) {
    state.errorStreak = 0;
    state.rateLimitStreak = 0;
    if (!state.loggedNotPublished) {
      state.loggedNotPublished = true;
      deps.log(`${LOG} ${source.label}: not published yet`);
    }
    return { outcome: "not_published", nextDelayMs: CYCLE_MS };
  }
  if (response.status === 429) {
    state.rateLimitStreak += 1;
    const retryAfter = response.retryAfterSeconds;
    const nextDelayMs = retryAfter !== null && Number.isFinite(retryAfter) && retryAfter >= 0
      ? Math.min(retryAfter * 1_000, RATE_LIMIT_CAP_MS)
      : Math.min(RATE_LIMIT_START_MS * 2 ** (state.rateLimitStreak - 1), RATE_LIMIT_CAP_MS);
    await logFailure(deps, "rate_limited");
    deps.log(`${LOG} ${source.label}: rate_limited; retry in ${minutes(nextDelayMs)}`);
    return { outcome: "rate_limited", nextDelayMs };
  }
  if (response.status !== 200) return fail(`http_${response.status}`);
  if (new TextEncoder().encode(response.body).byteLength > MAX_BODY_BYTES) return fail("too_large");

  let payload: unknown;
  try {
    payload = JSON.parse(response.body);
  } catch {
    return fail("bad_json");
  }
  if (!Array.isArray(payload)) return fail("bad_json");

  let parsed: ParsedFeed;
  let observations: FxCalendarObservation[];
  try {
    parsed = await deps.parse(payload);
    observations = parsed.rows.map(toObservation);
  } catch (error) {
    return fail(error instanceof CalendarParseRejected ? "parse_rejected" : "parser_unavailable");
  }

  try {
    await deps.store.recordFxCalendar(observations);
    await deps.store.recordFxCalendarFetch({
      source: FX_CALENDAR_SOURCE, status: "ok", eventCount: observations.length, coveredTo: parsed.coveredToMs,
    });
  } catch {
    return fail("store_unavailable");
  }
  state.errorStreak = 0;
  state.rateLimitStreak = 0;
  deps.log(`${LOG} ${source.label}: ${observations.length} events (${parsed.skippedHolidays} holidays, `
    + `${parsed.skippedAllLow} all-low skipped)`);
  return { outcome: "ok", nextDelayMs: CYCLE_MS };
}

export type CalendarLoopConfig = { thisWeek?: CalendarSource; nextWeek?: CalendarSource };

/**
 * Forever: this week, 30 s, next week, then the normal cycle (2 h + jitter). A failed or rate-limited
 * source waits its own backoff instead of the cycle. get_fx_calendar_status reports the covered_to of
 * the LATEST ok fetch, so next week must be the last ok write: after every ok this-week pass (including
 * a retry) next week runs again 30 s later, unless it is waiting out a rate limit.
 */
export async function runCalendarLoop(
  deps: CalendarFetcherDeps,
  config: CalendarLoopConfig,
  signal: AbortSignal,
): Promise<void> {
  const random = deps.random ?? Math.random;
  const thisWeek = config.thisWeek ?? THIS_WEEK;
  const nextWeek = config.nextWeek ?? NEXT_WEEK;
  const thisState = newSourceState();
  const nextState = newSourceState();
  const start = deps.now();
  let dueThis = start + START_DELAY_MS;
  let dueNext = dueThis + BETWEEN_SOURCES_MS;
  let nextRateLimited = false;
  const delayAfter = (result: PassResult) =>
    result.outcome === "ok" || result.outcome === "not_published"
      ? result.nextDelayMs + Math.floor(random() * JITTER_MS)
      : result.nextDelayMs;

  while (!signal.aborted) {
    const wait = Math.max(0, Math.min(dueThis, dueNext) - deps.now());
    if (wait > 0) {
      try {
        await deps.sleep(wait, signal);
      } catch {
        // An abort ends the loop below; any other sleep failure just re-evaluates the schedule.
      }
      continue;
    }
    const runThis = dueThis <= dueNext;
    try {
      if (runThis) {
        // This week first on a tie, so the next-week write is the latest ok fetch.
        const result = await runPass(deps, thisWeek, thisState, signal);
        dueThis = deps.now() + delayAfter(result);
        if (result.outcome === "ok" && !nextRateLimited) dueNext = deps.now() + BETWEEN_SOURCES_MS;
      } else {
        const result = await runPass(deps, nextWeek, nextState, signal);
        nextRateLimited = result.outcome === "rate_limited";
        dueNext = deps.now() + delayAfter(result);
      }
    } catch {
      if (signal.aborted) return;
      // runPass never throws otherwise; keep the loop alive regardless and never spin.
      if (runThis) dueThis = deps.now() + ERROR_START_MS;
      else dueNext = deps.now() + ERROR_START_MS;
    }
  }
}
