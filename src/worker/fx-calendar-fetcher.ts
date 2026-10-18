import {
  CalendarParseRejected,
  MAX_BODY_BYTES,
  USER_AGENT,
  runCalendarLoop,
  type CalendarFetcherDeps,
  type ParsedFeed,
  type ParsedRow,
} from "../lib/fx/calendar-fetcher.server";
import { getOperationalStore } from "../lib/operational/repository.server";
import { pythonServiceConfig } from "../lib/python-service.server";

// Runnable entrypoint for the economic calendar fetcher (#401 part 2b), a non-essential child of the
// worker host. It downloads the public weekly feeds, has the local Python analysis service parse them
// and writes only to the local operational database. It needs no hosted credential.
const PARSE_TIMEOUT_MS = 20_000;
const MAX_PARSE_RESPONSE_BYTES = 4 * 1024 * 1024;

/** Reads at most `limit` bytes plus one, so an oversized body is detected without buffering it all. */
async function readCapped(response: Response, limit: number): Promise<string> {
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    chunks.push(value);
    if (size > limit) {
      await reader.cancel().catch(() => {});
      break;
    }
  }
  return new TextDecoder().decode(Buffer.concat(chunks));
}

function retryAfterSeconds(header: string | null, now: number): number | null {
  if (!header) return null;
  if (/^\d+$/.test(header.trim())) return Number(header.trim());
  const at = Date.parse(header);
  return Number.isFinite(at) ? Math.max(0, Math.ceil((at - now) / 1_000)) : null;
}

const fetchFeed: CalendarFetcherDeps["fetchFeed"] = async (url, signal) => {
  const response = await fetch(url, {
    signal,
    headers: { "User-Agent": USER_AGENT, Accept: "application/json" },
  });
  const retryAfter = retryAfterSeconds(response.headers.get("retry-after"), Date.now());
  if (response.status !== 200) {
    await response.body?.cancel().catch(() => {});
    return { status: response.status, retryAfterSeconds: retryAfter, body: "" };
  }
  return { status: 200, retryAfterSeconds: retryAfter, body: await readCapped(response, MAX_BODY_BYTES) };
};

function createParse(config: { url: string; token: string }): CalendarFetcherDeps["parse"] {
  return async (payload) => {
    const response = await fetch(config.url, {
      method: "POST",
      redirect: "manual",
      signal: AbortSignal.timeout(PARSE_TIMEOUT_MS),
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        Authorization: `Bearer ${config.token}`,
      },
      body: JSON.stringify({ payload }),
    });
    if (response.status === 422) {
      await response.body?.cancel().catch(() => {});
      throw new CalendarParseRejected("rejected");
    }
    if (response.status !== 200) {
      await response.body?.cancel().catch(() => {});
      throw new Error("parser unavailable");
    }
    const data = JSON.parse(await readCapped(response, MAX_PARSE_RESPONSE_BYTES)) as Record<string, unknown>;
    const counts = [data["covered_to_ms"], data["skipped_holidays"], data["skipped_all_low"]];
    if (data["schema_version"] !== 1 || !Array.isArray(data["rows"]) || !counts.every(Number.isSafeInteger)) {
      throw new CalendarParseRejected("response shape");
    }
    return {
      rows: data["rows"] as ParsedRow[],
      coveredToMs: data["covered_to_ms"] as number,
      skippedHolidays: data["skipped_holidays"] as number,
      skippedAllLow: data["skipped_all_low"] as number,
    } satisfies ParsedFeed;
  };
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

try {
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The operational database is disabled (OPERATIONAL_DB_ENABLED=true is required)");
  let parseConfig: { url: string; token: string };
  try {
    parseConfig = pythonServiceConfig("/v1/fx/calendar/parse");
  } catch {
    throw new Error("The Python analysis service is not configured");
  }
  const controller = new AbortController();
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.once(signal, () => {
      controller.abort();
      process.exit(0);
    });
  }
  console.log("[fx-calendar] started");
  void runCalendarLoop({
    fetchFeed,
    parse: createParse(parseConfig),
    store,
    now: Date.now,
    sleep,
    log: (line) => console.log(line),
  }, {}, controller.signal).catch((error: unknown) => {
    console.error(`[fx-calendar] ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  });
} catch (error) {
  console.error(`[fx-calendar] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
