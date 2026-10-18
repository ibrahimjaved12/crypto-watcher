// Hourly forex market stage runner (#397), the forex twin of the crypto market stage, without any per-user
// wallet work. Once per completed hour it reads the subscribed instruments, their completed 1m BID bars, the
// broker's symbol specs, the collector health and the economic calendar from the local operational store, the
// saved open setups from the hosted main database, asks the Python forward service for ONE stage
// (POST /v1/fx/forward/stage, python/market_analysis/fx/stage.py) and commits it with commit_fx_stage.
//
// Never on partial inputs: the WHOLE stage is refused (retryable, nothing committed) when any subscribed
// symbol's spec is missing, its collector health is stale, or the calendar's covered_to does not reach the
// boundary plus the longest trade window. A symbol whose own inputs cannot be built honestly (no conversion
// price for its quote currency, an unsupported swap mode, bars that do not reach an open setup) is refused
// alone; the others are committed and the refusal is recorded in the stage's freshness, so the next stage
// starts its decision window before it (decisions are idempotent: deterministic ids, ON CONFLICT DO NOTHING).
// Every dependency is injected; the hosted client and the Python call are built by the entrypoint.
import type { FxGatewayRow, FxSymbolSpec, OperationalStore } from "../operational/types";
import { FOREX_DEFAULTS, FOREX_OPTIONAL } from "../market/instruments";

export const HOUR_MS = 3_600_000;
const MINUTE_MS = 60_000;
const DAY_MS = 24 * HOUR_MS;
/** The longest trade window: 4 x the 240-minute horizon (fx setups' time limit). */
export const FX_LONGEST_WINDOW_MS = 4 * 240 * MINUTE_MS;
/** UNVERIFIED placeholder: collector health older than this is stale. */
export const FX_HEALTH_MAX_AGE_MS = 5 * MINUTE_MS;
/** UNVERIFIED placeholder: while LIVE, the newest stored bar must be at most this old at the boundary. */
export const FX_LIVE_BAR_MAX_LAG_MS = 2 * MINUTE_MS;
/** The decision window never reaches further back than this (catch-up after refusals or downtime). */
export const FX_MAX_CATCHUP_MS = DAY_MS;
/** One get_fx_bars page: the function's own maximum. A page shorter than this means the history ends. */
export const FX_BAR_PAGE_SIZE = 20_000;
/**
 * Minute bars of warm-up one symbol needs for the 240-minute strategies (#440). Derived from
 * python/market_analysis/benchmark/ta_strategies.py `first_signal_index`: the widest strategy,
 * `ema_cross_50_200`, has index 402 (`ema_warmup(200)` = 2 * 201), and a signal needs MORE candles than that,
 * so 403 candles. A forex bar exists only while the market is open and a 240-minute candle holds 240 open
 * minutes, so 403 candles are 403 * 240 bars; plus a margin of one day (1440) for the decision window and the
 * entry minutes of open setups. The next widest, `ema_cross_20_50`, needs 103 candles (24,720 bars).
 */
export const FX_WARMUP_CANDLES_240 = 402 + 1;
export const FX_WARMUP_MINUTES = FX_WARMUP_CANDLES_240 * 240 + 1440;
/**
 * Hard cap on bars sent per symbol (the request body to /v1/fx/forward/stage grows with it: about 205 bytes
 * per row, so about 20.5 MB per symbol and about 82 MB for four). It must never be below FX_WARMUP_MINUTES
 * (98,160), or ema_cross_50_200 on 240-minute candles reports insufficient_history forever (#442 raised it
 * from 60,000, which was only 250 candles). The invariant is enforced at module load below.
 */
export const FX_MAX_BARS_PER_SYMBOL = 100_000;
if (FX_MAX_BARS_PER_SYMBOL < FX_WARMUP_MINUTES) {
  throw new Error(`FX_MAX_BARS_PER_SYMBOL (${FX_MAX_BARS_PER_SYMBOL}) is below FX_WARMUP_MINUTES (${FX_WARMUP_MINUTES})`);
}
export const FX_CALENDAR_SOURCE = "forexfactory";
const LOG = "[forex-hourly]";
const OK_HEALTH = new Set(["LIVE", "MARKET_CLOSED"]);

/** The response of POST /v1/fx/forward/stage (python/market_analysis/fx/stage.py). */
export type FxStageResponse = {
  schema_version: 1;
  versions: Record<string, string>;
  params_hash: string;
  processed_to_ms: number;
  stage: { id: string; boundary_ms: number; versions: unknown; params_hash: string; payload: unknown; freshness: unknown };
  signals: unknown[];
  suppressed: unknown[];
  setups: unknown[];
  outcomes: unknown[];
  events: unknown[];
};

export type FxStageHosted = {
  /** Newest committed stages first: boundary and freshness. */
  recentStages(limit: number): Promise<{ boundary_ms: number; freshness: unknown }[]>;
  /** Saved FxSetup dicts of the tradeable setups without a final outcome (fx_market_open_setups). */
  openSetups(): Promise<Record<string, unknown>[]>;
  /** Stored outcomes of these setups. */
  outcomes(setupIds: string[]): Promise<{ setup_id: string; status: string; final: boolean; payload: unknown }[]>;
  /** commit_fx_stage: the stage id stored for the boundary (an earlier one when it was already committed). */
  commit(stage: FxStageResponse["stage"], records: Record<string, unknown[]>): Promise<string>;
};

export type FxStageDeps = {
  now(): number;
  store: Pick<OperationalStore, "getFxSubscriptions" | "getFxBars" | "getFxSymbolSpecs" | "getFxCalendarEvents"
    | "getFxCalendarStatus" | "getFxCollectorHealth">;
  hosted: FxStageHosted;
  callStage(body: Record<string, unknown>): Promise<FxStageResponse>;
  strategyIds?: string[];
  log(line: string): void;
  error(line: string): void;
};

export type FxHourlyResult = {
  boundary: number;
  status: "ok" | "already_done" | "skipped_stale" | "no_subscriptions" | "failed";
  reason?: string;
  /** Symbols refused on their own (their inputs were not available); the others were committed. */
  failed: Record<string, string>;
  evaluated: string[];
};

// ---------------------------------------------------------------- exact numbers (never floats on the wire)

/** A JS number as its exact shortest decimal text, without exponent ("1e-7" -> "0.0000001"). */
export function exactDecimal(value: number): string {
  if (!Number.isFinite(value)) throw new Error("not a finite number");
  const text = String(value);
  const match = /^(-?)(\d+)(?:\.(\d+))?e([+-]\d+)$/.exec(text);
  if (!match) return text;
  const [, sign, int, frac = "", exp] = match;
  const digits = int! + frac;
  const point = int!.length + Number(exp);
  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`.replace(/\.?0+$/, "") || "0";
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

/** Exact decimal text -> [numerator, denominator] as bigints. */
function fraction(text: string): [bigint, bigint] {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text);
  if (!match) throw new Error("not a decimal");
  const [, sign, int, frac = ""] = match;
  const numerator = BigInt(`${int}${frac}`) * (sign ? -1n : 1n);
  return [numerator, 10n ** BigInt(frac.length)];
}

const fractionText = (numerator: bigint, denominator: bigint) =>
  denominator === 1n ? String(numerator) : `${numerator}/${denominator}`;

/** Mid of one bar in exact fraction text: (round(bid * 10^d) * 2 + spread_points) / (2 * 10^d). */
export function midFraction(bar: Pick<FxGatewayRow, "close" | "spread_points">, digits: number): [bigint, bigint] {
  const scale = 10n ** BigInt(digits);
  const [n, d] = fraction(exactDecimal(bar.close));
  // round(bid * 10^digits) on the exact decimal: half away from zero is fine, bids are positive.
  const points = (n * scale * 2n + d) / (2n * d);
  return [points * 2n + BigInt(bar.spread_points), 2n * scale];
}

// ---------------------------------------------------------------- per-symbol inputs

function expectedCurrencies(symbol: string): { base: string; quote: string } | null {
  const known = [...FOREX_DEFAULTS, ...FOREX_OPTIONAL].find((item) => item.instrumentId.split(":")[1] === symbol);
  if (known) return { base: known.base, quote: known.quote };
  return /^[A-Z]{6}$/.test(symbol) ? { base: symbol.slice(0, 3), quote: symbol.slice(3) } : null;
}

/** MT5 swap modes: 0 disabled, 1 in points (quote currency per base unit = swap * point); others not yet. */
function swapRates(spec: FxSymbolSpec): { long_per_unit: string; short_per_unit: string } {
  if (spec.swapMode === 0) return { long_per_unit: "0", short_per_unit: "0" };
  if (spec.swapMode !== 1) throw new Error(`swap mode ${spec.swapMode} is not supported yet`);
  const scale = 10n ** BigInt(spec.digits);
  const rate = (value: number) => {
    const [n, d] = fraction(exactDecimal(value));
    return fractionText(n, d * scale);
  };
  return { long_per_unit: rate(spec.swapLong), short_per_unit: rate(spec.swapShort) };
}

/** 1 when the quote currency is the account currency, else the mid of the same feed's conversion pair. */
export async function quoteToAccount(deps: { store: Pick<OperationalStore, "getFxBars"> }, spec: FxSymbolSpec, specs: FxSymbolSpec[]): Promise<string> {
  const quote = spec.profitCurrency;
  const account = spec.accountCurrency;
  if (quote === account) return "1";
  for (const [pair, inverse] of [[`${quote}${account}`, false], [`${account}${quote}`, true]] as const) {
    const conversion = specs.find((item) => item.venue === spec.venue && item.instrumentId === `fx:${pair}`);
    if (!conversion) continue;
    const [bar] = (await deps.store.getFxBars(spec.venue, conversion.instrumentId, { limit: 1 })).slice(-1);
    if (!bar) continue;
    const [n, d] = midFraction(bar, conversion.digits);
    return inverse ? fractionText(d, n) : fractionText(n, d);
  }
  throw new Error(`no conversion price from ${quote} to ${account} on ${spec.venue}`);
}

export function gatewayRow(row: FxGatewayRow) {
  return { time_basis: row.time_basis, venue: row.venue, native_symbol: row.native_symbol, transport: row.transport,
    open_time_ms: row.open_time_ms, open: row.open, high: row.high, low: row.low, close: row.close,
    spread_points: row.spread_points, tick_volume: row.tick_volume };
}

/** The previous resolution the Python stage accepts: a stored outcome payload without the stats flag. */
function previousResolution(stored: { payload: unknown }[]): Record<string, unknown> | null {
  const payloads = stored.map((row) => row.payload).filter((p): p is Record<string, unknown> =>
    !!p && typeof p === "object" && !Array.isArray(p));
  const pick = payloads.find((p) => p["status"] === "open") ?? payloads.find((p) => p["status"] === "pending");
  if (!pick) return null;
  const resolution: Record<string, unknown> = { ...pick };
  delete resolution["excluded_from_stats"];
  return resolution;
}

/**
 * The newest `neededBars` bars of a series, oldest first (capped at FX_MAX_BARS_PER_SYMBOL). Reads the newest
 * page, then, while the total is below `neededBars` and the last page was full, the next older page with
 * `toMs` = the oldest open time of the previous page (EXCLUSIVE, so no row repeats); a short page means the
 * history ends. Duplicate open times are dropped defensively.
 */
export async function readBarsPaged(
  deps: Pick<FxStageDeps, "store">, venue: string, instrumentId: string, neededBars: number,
): Promise<FxGatewayRow[]> {
  const wanted = Math.min(Math.max(1, Math.floor(neededBars)), FX_MAX_BARS_PER_SYMBOL);
  const seen = new Set<number>();
  let bars: FxGatewayRow[] = [];
  let toMs: number | undefined;
  for (;;) {
    const page = await deps.store.getFxBars(venue, instrumentId,
      toMs === undefined ? { limit: FX_BAR_PAGE_SIZE } : { limit: FX_BAR_PAGE_SIZE, toMs });
    const fresh = page.filter((row) => !seen.has(row.open_time_ms));
    for (const row of fresh) seen.add(row.open_time_ms);
    bars = [...fresh, ...bars];
    if (page.length < FX_BAR_PAGE_SIZE || fresh.length === 0 || bars.length >= wanted) break;
    toMs = page[0]!.open_time_ms;
  }
  return bars.length > wanted ? bars.slice(bars.length - wanted) : bars;
}

/** The venue_symbol record of the Python forex endpoints (stage and wallet step). */
export function fxVenueSymbol(spec: FxSymbolSpec) {
  return { venue: spec.venue, native_symbol: spec.nativeSymbol, digits: spec.digits, tick_size: exactDecimal(spec.tickSize) };
}

/** The contract record of the Python forex endpoints, as exact decimal strings. */
export function fxContract(spec: FxSymbolSpec) {
  return { contract_size: exactDecimal(spec.contractSize), tick_size: exactDecimal(spec.tickSize),
    min_lot: exactDecimal(spec.volumeMin), lot_step: exactDecimal(spec.volumeStep), base: spec.baseCurrency,
    quote: spec.profitCurrency };
}

async function symbolInput(deps: FxStageDeps, spec: FxSymbolSpec, specs: FxSymbolSpec[],
                           saved: Record<string, unknown>[], outcomes: Map<string, { payload: unknown }[]>) {
  const symbol = spec.instrumentId.split(":")[1] ?? "";
  const expected = expectedCurrencies(symbol);
  if (!expected) throw new Error("not a supported forex symbol");
  if (expected.base !== spec.baseCurrency || expected.quote !== spec.profitCurrency)
    throw new Error(`broker currencies ${spec.baseCurrency}/${spec.profitCurrency} differ from ${expected.base}/${expected.quote}`);
  const bars = await readBarsPaged(deps, spec.venue, spec.instrumentId, FX_WARMUP_MINUTES);
  if (!bars.length) throw new Error("no bars");
  if (bars.some((row) => row.native_symbol !== spec.nativeSymbol)) throw new Error("bars carry another native symbol than the spec");
  const first = bars[0]!.open_time_ms;
  const openSetups = saved.map((setup) => {
    if (Number(setup["entry_ms"]) < first) throw new Error("stored bars do not reach an open setup's entry");
    return { setup, previous: previousResolution(outcomes.get(String(setup["setup_id"])) ?? []) };
  });
  return {
    symbol,
    venue_symbol: fxVenueSymbol(spec),
    contract: fxContract(spec),
    swap_rates: swapRates(spec),
    quote_to_account: await quoteToAccount(deps, spec, specs),
    bars: bars.map(gatewayRow),
    open_setups: openSetups,
  };
}

// ---------------------------------------------------------------- the stage

/** Stage-level refusals: a missing spec, stale collector health, or calendar coverage too short. */
export function stageRefusals(input: {
  boundary: number;
  now: number;
  subscriptions: { venue: string; instrumentId: string }[];
  specs: FxSymbolSpec[];
  health: { venue: string; instrument_id: string; status: string; updated_at_ms: number; last_bar_open_time_ms: number | null }[];
  calendar: { source: string; last_ok_covered_to_ms: number | null }[];
}): string[] {
  const reasons: string[] = [];
  for (const sub of input.subscriptions) {
    const symbol = sub.instrumentId.split(":")[1] ?? sub.instrumentId;
    if (!input.specs.some((spec) => spec.venue === sub.venue && spec.instrumentId === sub.instrumentId))
      reasons.push(`${symbol}: no symbol spec`);
    const row = input.health.find((item) => item.venue === sub.venue && item.instrument_id === sub.instrumentId);
    if (!row) reasons.push(`${symbol}: no collector health`);
    else if (!OK_HEALTH.has(row.status)) reasons.push(`${symbol}: collector ${row.status}`);
    else if (input.now - row.updated_at_ms > FX_HEALTH_MAX_AGE_MS)
      reasons.push(`${symbol}: collector health ${Math.round((input.now - row.updated_at_ms) / 1000)}s old`);
    else if (row.status === "LIVE" && (row.last_bar_open_time_ms === null
      || row.last_bar_open_time_ms < input.boundary - FX_LIVE_BAR_MAX_LAG_MS))
      reasons.push(`${symbol}: newest bar older than the boundary`);
  }
  const calendar = input.calendar.find((item) => item.source === FX_CALENDAR_SOURCE);
  const needed = input.boundary + FX_LONGEST_WINDOW_MS;
  if (!calendar || calendar.last_ok_covered_to_ms === null) reasons.push("calendar: never fetched");
  else if (calendar.last_ok_covered_to_ms < needed)
    reasons.push(`calendar: covered to ${new Date(calendar.last_ok_covered_to_ms).toISOString()}, needs ${new Date(needed).toISOString()}`);
  return reasons;
}

/** The decision window start: the last stage in which every subscribed symbol was evaluated (bounded). */
export function decisionStart(boundary: number, recent: { boundary_ms: number; freshness: unknown }[]): number {
  const floor = boundary - FX_MAX_CATCHUP_MS;
  if (!recent.length) return boundary - HOUR_MS;
  const complete = recent.find((stage) => {
    const refused = (stage.freshness as { refused?: Record<string, unknown> } | null)?.refused;
    return !refused || Object.keys(refused).length === 0;
  });
  return Math.max(floor, complete ? complete.boundary_ms : recent.at(-1)!.boundary_ms - HOUR_MS);
}

export async function runFxHourly(deps: FxStageDeps): Promise<FxHourlyResult> {
  const now = deps.now();
  const boundary = Math.floor(now / HOUR_MS) * HOUR_MS;
  const key = `hour:${boundary}`;
  const result = (status: FxHourlyResult["status"], extra: Partial<FxHourlyResult> = {}): FxHourlyResult =>
    ({ boundary, status, failed: {}, evaluated: [], ...extra });

  const recent = await deps.hosted.recentStages(48);
  if (recent[0] && recent[0].boundary_ms >= boundary) {
    deps.log(`${LOG} ${key} already_done`);
    return result("already_done");
  }
  const subscriptions = await deps.store.getFxSubscriptions();
  if (!subscriptions.length) {
    deps.log(`${LOG} ${key} no forex subscriptions`);
    return result("no_subscriptions");
  }
  const [specs, health, calendar] = await Promise.all([
    deps.store.getFxSymbolSpecs(), deps.store.getFxCollectorHealth(), deps.store.getFxCalendarStatus(),
  ]);
  const refusals = stageRefusals({ boundary, now, subscriptions, specs, health, calendar });
  if (refusals.length) {
    const reason = refusals.join("; ");
    deps.log(`${LOG} ${key} skipped_stale: ${reason}`);
    return result("skipped_stale", { reason });
  }

  const fromMs = decisionStart(boundary, recent);
  const saved = await deps.hosted.openSetups();
  const outcomes = new Map<string, { payload: unknown }[]>();
  for (const row of await deps.hosted.outcomes(saved.map((setup) => String(setup["setup_id"])))) {
    if (row.final) continue;
    outcomes.set(row.setup_id, [...(outcomes.get(row.setup_id) ?? []), row]);
  }
  const failed: Record<string, string> = {};
  const symbols = [];
  for (const sub of subscriptions) {
    const spec = specs.find((item) => item.venue === sub.venue && item.instrumentId === sub.instrumentId)!;
    const symbol = sub.instrumentId.split(":")[1] ?? sub.instrumentId;
    try {
      symbols.push(await symbolInput(deps, spec, specs, saved.filter((setup) => setup["symbol"] === symbol), outcomes));
    } catch (error) {
      failed[symbol] = error instanceof Error ? error.message : String(error);
      deps.error(`${LOG} ${key} ${symbol} refused: ${failed[symbol]}`);
    }
  }
  if (!symbols.length) return result("failed", { failed, reason: "no symbol could be evaluated" });

  const events = await deps.store.getFxCalendarEvents(fromMs - DAY_MS, boundary + DAY_MS, boundary);
  const response = await deps.callStage({
    boundary_ms: boundary, from_ms: fromMs, symbols, calendar_events: events,
    ...(deps.strategyIds ? { strategy_ids: deps.strategyIds } : {}),
  });
  const stage = { ...response.stage, freshness: { refused: failed, evaluated: symbols.map((item) => item.symbol) } };
  const stored = await deps.hosted.commit(stage, {
    signals: response.signals, suppressed: response.suppressed, setups: response.setups,
    outcomes: response.outcomes, events: response.events,
  });
  const evaluated = symbols.map((item) => item.symbol);
  if (stored !== stage.id) {
    deps.log(`${LOG} ${key} already_done`);
    return result("already_done", { failed, evaluated });
  }
  deps.log(`${LOG} ${key} ok from ${new Date(fromMs).toISOString()} signals=${response.signals.length} `
    + `suppressed=${response.suppressed.length} setups=${response.setups.length} outcomes=${response.outcomes.length}`
    + (Object.keys(failed).length ? ` refused=${Object.keys(failed).join(",")}` : ""));
  return result("ok", { failed, evaluated });
}

// ---------------------------------------------------------------- hosted client and Python call

type Query = PromiseLike<{ data: unknown; error: { message: string } | null }> & {
  select(columns: string): Query;
  order(column: string, options: { ascending: boolean }): Query;
  limit(count: number): Query;
  in(column: string, values: string[]): Query;
};
export type HostedFxStageClient = {
  from(table: "fx_market_stages" | "fx_market_open_setups" | "fx_market_outcomes"): Query;
  rpc(name: "commit_fx_stage", args: { p_stage: unknown; p_records: unknown }): PromiseLike<{
    data: unknown;
    error: { message: string; code?: string } | null;
  }>;
};

function rows<T>(reply: { data: unknown; error: { message: string } | null }, what: string): T[] {
  if (reply.error) throw new Error(`${what} read failed: ${reply.error.message}`);
  if (!Array.isArray(reply.data)) throw new Error(`${what} read returned no rows array`);
  return reply.data as T[];
}

/** Hosted reads and the commit through the service client (service_role: SELECT on the fx_market_* tables). */
export function createHostedFxStage(client: HostedFxStageClient): FxStageHosted {
  return {
    async recentStages(limit) {
      return rows(await client.from("fx_market_stages").select("boundary_ms, freshness")
        .order("boundary_ms", { ascending: false }).limit(limit), "fx_market_stages");
    },
    async openSetups() {
      const data = rows<{ payload: Record<string, unknown> }>(
        await client.from("fx_market_open_setups").select("payload"), "fx_market_open_setups");
      return data.map((row) => row.payload);
    },
    async outcomes(setupIds) {
      const out = [];
      for (let i = 0; i < setupIds.length; i += 100) {
        out.push(...rows<{ setup_id: string; status: string; final: boolean; payload: unknown }>(
          await client.from("fx_market_outcomes").select("setup_id, status, final, payload")
            .in("setup_id", setupIds.slice(i, i + 100)), "fx_market_outcomes"));
      }
      return out;
    },
    async commit(stage, records) {
      const { data, error } = await client.rpc("commit_fx_stage", { p_stage: stage, p_records: records });
      if (error) throw new Error(`commit_fx_stage failed${error.code ? ` (${error.code})` : ""}: ${error.message}`);
      if (typeof data !== "string") throw new Error("commit_fx_stage returned no stage id");
      return data;
    },
  };
}

export function createFxStageCall(config: { url: string; token: string }, send: typeof fetch = fetch,
                                  timeoutMs = 120_000): FxStageDeps["callStage"] {
  return async (body) => {
    const response = await send(config.url, {
      method: "POST",
      redirect: "manual",
      signal: AbortSignal.timeout(timeoutMs),
      headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let detail = "";
      try { detail = String(((await response.json()) as { detail?: unknown }).detail ?? "").slice(0, 300); }
      catch { /* no JSON body */ }
      throw new Error(`Forex stage failed with HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
    }
    const data = (await response.json()) as FxStageResponse;
    if (data?.schema_version !== 1 || typeof data.stage?.id !== "string") throw new Error("Invalid forex stage response");
    return data;
  };
}
