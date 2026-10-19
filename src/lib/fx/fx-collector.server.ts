// Forex collector (#453, part of #374), a worker-host child. It polls the MT5 gateway (mt5/gateway/mt5_gateway.py,
// #452) over HTTP once a minute, stores the completed 1-minute BID bars and the symbol specs in the local
// operational store, and reports collector health per instrument. It never talks to MetaTrader itself and never
// sees account data. Every dependency is injected; the entrypoint (src/worker/forex-collector.ts) builds them.
//
// One collector owns the store at a time (claimFxCollectorLease, 90 s, renewed every pass). A pass never throws:
// every failure is logged with the "[fx-collector]" prefix and shows in the health rows.
import type {
  FxBar, FxCollectorHealthInput, FxCollectorHealthStatus, FxSubscription, FxSymbolSpec, OperationalStore,
} from "../operational/types";
import { fxNextOpenMs, isFxOpen } from "../market/trading-calendar";

export const MINUTE_MS = 60_000;
const DAY_MS = 86_400_000;
const LOG = "[fx-collector]";
export const FX_LEASE_SECONDS = 90;
/** The gateway's cap per request. */
export const FX_PAGE_MINUTES = 5_000;
/** record_fx_bars takes 1..1000 rows per call. */
const WRITE_BATCH = 1_000;
/** A span up to this many minutes is a live poll; a longer one is history (backfill or gap fill). */
export const FX_POLL_MAX_SPAN_MINUTES = 10;
/** Pages fetched per symbol and pass, so one pass stays short; the rest is the next pass (RECOVERING). */
export const FX_MAX_PAGES_PER_PASS = 4;
export const FX_LIVE_MAX_LAG_MS = 3 * MINUTE_MS;
export const FX_STALE_LAG_MS = 10 * MINUTE_MS;
export const FX_SPEC_INTERVAL_MS = 6 * 3_600_000;
export const FX_SPEC_BATCH = 50;
export const FX_DEFAULT_BACKFILL_DAYS = 5;
export const FX_DEFAULT_VENUE = "exness-mt5-demo";

export type GatewayReply = { status: number; body: unknown };

export type FxCollectorDeps = {
  store: Pick<OperationalStore, "getFxSubscriptions" | "recordFxBars" | "getFxBarCoverage" | "recordFxCollectorHealth"
    | "claimFxCollectorLease" | "renewFxCollectorLease" | "releaseFxCollectorLease" | "recordFxSymbolSpecs">;
  /** GET a gateway path (with query); resolves with the status and parsed JSON body, rejects when unreachable. */
  gateway(path: string): Promise<GatewayReply>;
  now(): number;
  /** The lease owner: a UUID (claim_fx_collector_lease takes one). */
  instanceId: string;
  /** The account currency of the demo account: the gateway never reads account data, so the owner supplies it. */
  accountCurrency?: string | undefined;
  backfillDays?: number;
  log(line: string): void;
  error(line: string): void;
};

export type FxCollectorState = {
  leased: boolean;
  /** When the next spec refresh is due (0 = at the next pass). */
  nextSpecsAt: number;
  /** Symbols already reported as having a missing or incomplete spec (once per process). */
  warnedSpecs: Set<string>;
  warnedNoCurrency: boolean;
  /** The "another collector owns the store" line has been logged (it is repeated only after a change). */
  warnedLease: boolean;
};

export function newFxCollectorState(): FxCollectorState {
  return { leased: false, nextSpecsAt: 0, warnedSpecs: new Set(), warnedNoCurrency: false, warnedLease: false };
}

export type FxPassResult = {
  leased: boolean;
  stored: Record<string, number>;
  skipped: Record<string, number>;
  health: FxCollectorHealthInput[];
};

const errorText = (error: unknown) => (error instanceof Error ? error.message : String(error));
const symbolOf = (instrumentId: string) => instrumentId.split(":")[1] ?? instrumentId;
const object = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const gatewayCode = (reply: GatewayReply) =>
  object(reply.body) && typeof reply.body["error"] === "string" ? reply.body["error"] : `http_${reply.status}`;

// ---------------------------------------------------------------- rows

/** One gateway bar row -> FxBar, or null when it fails the local checks (never stored). */
export function barFromRow(row: unknown, sub: FxSubscription, window: { fromMs: number; toMs: number },
                           transport: FxBar["transport"], receivedAt: number): FxBar | null {
  if (!object(row)) return null;
  const { open_time_ms: openTime, open, high, low, close, spread_points: spread, tick_volume: volume } = row;
  if (row["time_basis"] !== "utc" || row["native_symbol"] !== sub.nativeSymbol) return null;
  if (typeof openTime !== "number" || !Number.isSafeInteger(openTime) || openTime % MINUTE_MS !== 0) return null;
  if (openTime < window.fromMs || openTime >= window.toMs) return null;
  if (typeof spread !== "number" || !Number.isSafeInteger(spread) || spread < 0) return null;
  if (typeof volume !== "number" || !Number.isSafeInteger(volume) || volume < 0) return null;
  for (const price of [open, high, low, close])
    if (typeof price !== "number" || !Number.isFinite(price) || price <= 0) return null;
  const [o, h, l, c] = [open, high, low, close] as number[];
  if (h! < Math.max(o!, c!, l!) || l! > Math.min(o!, c!, h!)) return null;
  return { venue: sub.venue, instrumentId: sub.instrumentId, symbol: symbolOf(sub.instrumentId),
    nativeSymbol: sub.nativeSymbol, openTime, open: o!, high: h!, low: l!, close: c!, spreadPoints: spread,
    tickVolume: volume, transport, receivedAt };
}

// ---------------------------------------------------------------- specs

const NUMBER_FIELDS = ["point", "trade_tick_size", "trade_tick_value", "trade_contract_size", "volume_min",
  "volume_max", "volume_step", "swap_long", "swap_short"] as const;
const INT_FIELDS = ["digits", "trade_mode", "swap_mode", "swap_rollover3days"] as const;
const TEXT_FIELDS = ["currency_base", "currency_profit", "currency_margin"] as const;

/** The gateway's spec -> the spec store's row, or the reason it is incomplete. */
export function specFromGateway(sub: FxSubscription, raw: unknown, accountCurrency: string):
  { spec: Omit<FxSymbolSpec, "observedAt" | "specHash"> } | { problem: string } {
  if (!object(raw)) return { problem: "the terminal does not know the symbol" };
  const missing: string[] = [];
  for (const name of NUMBER_FIELDS)
    if (typeof raw[name] !== "number" || !Number.isFinite(raw[name])) missing.push(name);
  for (const name of INT_FIELDS)
    if (typeof raw[name] !== "number" || !Number.isSafeInteger(raw[name])) missing.push(name);
  for (const name of TEXT_FIELDS)
    if (typeof raw[name] !== "string" || !raw[name]) missing.push(name);
  if (missing.length) return { problem: `incomplete spec (${missing.join(", ")})` };
  const digits = raw["digits"] as number;
  if (digits < 0 || raw["point"] !== Number(`1e-${digits}`)) return { problem: "point is not 10**-digits" };
  for (const name of ["trade_tick_size", "trade_contract_size", "volume_min", "volume_step"] as const)
    if ((raw[name] as number) <= 0) return { problem: `${name} is not positive` };
  return { spec: {
    venue: sub.venue, instrumentId: sub.instrumentId, nativeSymbol: sub.nativeSymbol, digits,
    point: raw["point"] as number, tickSize: raw["trade_tick_size"] as number,
    tickValue: raw["trade_tick_value"] as number, contractSize: raw["trade_contract_size"] as number,
    volumeMin: raw["volume_min"] as number, volumeMax: raw["volume_max"] as number,
    volumeStep: raw["volume_step"] as number, baseCurrency: raw["currency_base"] as string,
    profitCurrency: raw["currency_profit"] as string, marginCurrency: raw["currency_margin"] as string,
    accountCurrency, tradeMode: raw["trade_mode"] as number, swapMode: raw["swap_mode"] as number,
    swapLong: raw["swap_long"] as number, swapShort: raw["swap_short"] as number,
    swapRollover3days: raw["swap_rollover3days"] as number } };
}

async function refreshSpecs(deps: FxCollectorDeps, state: FxCollectorState, subs: FxSubscription[]): Promise<void> {
  const now = deps.now();
  if (now < state.nextSpecsAt || subs.length === 0) return;
  const currency = deps.accountCurrency;
  if (!currency || !/^[A-Z]{3}$/.test(currency)) {
    if (!state.warnedNoCurrency) {
      state.warnedNoCurrency = true;
      deps.error(`${LOG} FX_ACCOUNT_CURRENCY is not set: symbol specs are not recorded (the gateway does not read account data)`);
    }
    return;
  }
  const rows: Omit<FxSymbolSpec, "observedAt" | "specHash">[] = [];
  const reported: string[] = [];
  try {
    for (let i = 0; i < subs.length; i += FX_SPEC_BATCH) {
      const batch = subs.slice(i, i + FX_SPEC_BATCH);
      const names = batch.map((sub) => encodeURIComponent(sub.nativeSymbol)).join(",");
      const reply = await deps.gateway(`/v1/specs?symbols=${names}`);
      if (reply.status !== 200 || !object(reply.body) || !object(reply.body["specs"]))
        throw new Error(`/v1/specs answered ${gatewayCode(reply)}`);
      const specs = reply.body["specs"];
      for (const sub of batch) {
        const built = specFromGateway(sub, specs[sub.nativeSymbol], currency);
        if ("spec" in built) rows.push(built.spec);
        else {
          reported.push(symbolOf(sub.instrumentId));
          if (!state.warnedSpecs.has(sub.instrumentId)) {
            state.warnedSpecs.add(sub.instrumentId);
            deps.error(`${LOG} ${symbolOf(sub.instrumentId)}: no usable symbol spec: ${built.problem}`);
          }
        }
      }
    }
    if (rows.length) await deps.store.recordFxSymbolSpecs(rows);
    state.nextSpecsAt = now + FX_SPEC_INTERVAL_MS;
    deps.log(`${LOG} specs: ${rows.length} recorded${reported.length ? `, unusable: ${reported.join(", ")}` : ""}`);
  } catch (error) {
    // Retried at the next pass.
    deps.error(`${LOG} specs failed: ${errorText(error)}`);
  }
}

// ---------------------------------------------------------------- bars and health

type SymbolOutcome = {
  stored: number;
  skipped: number;
  newestOpen: number | null;
  /** A gateway or terminal problem (the health message), or null. */
  problem: { status: "UNAVAILABLE" | "LOGIN_FAILED"; message: string } | null;
  /** Pages remain for the next pass (a backfill or gap fill is still in progress). */
  backfilling: boolean;
};

async function collectSymbol(deps: FxCollectorDeps, sub: FxSubscription, newestStored: number | null,
                             now: number): Promise<SymbolOutcome> {
  const outcome: SymbolOutcome = { stored: 0, skipped: 0, newestOpen: newestStored, problem: null, backfilling: false };
  const toMs = Math.floor(now / MINUTE_MS) * MINUTE_MS; // exclusive: the minute in progress is not closed
  const days = deps.backfillDays ?? FX_DEFAULT_BACKFILL_DAYS;
  let from = newestStored !== null ? newestStored + MINUTE_MS : toMs - days * DAY_MS;
  // Nothing to read when the whole span lies in a closed market (the gateway has no minutes there).
  if (from >= toMs || fxNextOpenMs(from) >= toMs) return outcome;
  for (let page = 0; from < toMs; page++) {
    if (page >= FX_MAX_PAGES_PER_PASS) { outcome.backfilling = true; break; }
    const pageTo = Math.min(toMs, from + FX_PAGE_MINUTES * MINUTE_MS);
    const transport: FxBar["transport"] = pageTo - from <= FX_POLL_MAX_SPAN_MINUTES * MINUTE_MS ? "poll" : "history";
    const path = `/v1/bars?symbol=${encodeURIComponent(sub.nativeSymbol)}&from_ms=${from}&to_ms=${pageTo}`;
    const reply = await deps.gateway(path);
    if (reply.status !== 200 || !object(reply.body) || !Array.isArray(reply.body["bars"])) {
      outcome.problem = { status: "UNAVAILABLE", message: gatewayCode(reply) };
      return outcome;
    }
    const bars: FxBar[] = [];
    for (const row of reply.body["bars"]) {
      const bar = barFromRow(row, sub, { fromMs: from, toMs: pageTo }, transport, now);
      if (bar) bars.push(bar); else outcome.skipped++;
    }
    bars.sort((a, b) => a.openTime - b.openTime);
    for (let i = 0; i < bars.length; i += WRITE_BATCH) await deps.store.recordFxBars(bars.slice(i, i + WRITE_BATCH));
    outcome.stored += bars.length;
    const newest = bars.at(-1)?.openTime;
    if (newest !== undefined) outcome.newestOpen = Math.max(outcome.newestOpen ?? 0, newest);
    from = pageTo;
  }
  return outcome;
}

/** Health of one instrument after a pass (see the statuses in FxCollectorHealthStatus). */
export function fxHealth(sub: FxSubscription, now: number, newestOpen: number | null,
                         outcome: Pick<SymbolOutcome, "problem" | "backfilling"> | null,
                         gatewayProblem: { status: "UNAVAILABLE" | "LOGIN_FAILED"; message: string } | null,
): FxCollectorHealthInput {
  const lag = newestOpen === null ? null : now - (newestOpen + MINUTE_MS);
  const row = (status: FxCollectorHealthStatus, errorMessage: string | null): FxCollectorHealthInput =>
    ({ venue: sub.venue, instrumentId: sub.instrumentId, status, lastBarOpenTime: newestOpen, lagMs: lag, errorMessage });
  const problem = gatewayProblem ?? outcome?.problem ?? null;
  if (problem) return row(problem.status, problem.message);
  if (!isFxOpen(now)) return row("MARKET_CLOSED", null);
  if (outcome?.backfilling) return row("RECOVERING", null); // pages remain: the age of the newest bar is expected
  if (newestOpen === null) return row("STALE", "no bars stored");
  if (lag! > FX_STALE_LAG_MS) return row("STALE", null);
  if (lag! > FX_LIVE_MAX_LAG_MS) return row("RECOVERING", null);
  return row("LIVE", null);
}

/** One pass. Never throws. */
export async function runFxCollectorPass(deps: FxCollectorDeps, state: FxCollectorState): Promise<FxPassResult> {
  const result: FxPassResult = { leased: false, stored: {}, skipped: {}, health: [] };
  try {
    // The lease: renew, or claim again after losing it; without it this process does nothing.
    let leased = state.leased ? await deps.store.renewFxCollectorLease(deps.instanceId, FX_LEASE_SECONDS) : false;
    if (!leased) leased = await deps.store.claimFxCollectorLease(deps.instanceId, FX_LEASE_SECONDS);
    if (leased !== state.leased || (!leased && !state.warnedLease)) {
      deps.log(`${LOG} lease ${leased ? "held" : "not held; another collector owns the store"}`);
      state.warnedLease = !leased;
    }
    state.leased = leased;
    if (!leased) return result;
    result.leased = true;

    const subs = await deps.store.getFxSubscriptions();
    if (subs.length === 0) {
      deps.log(`${LOG} no forex subscriptions (run the child once with --subscribe)`);
      return result;
    }
    const now = deps.now();

    // The terminal state decides first: a signed-out terminal or an unreachable gateway is not "stale".
    let gatewayProblem: { status: "UNAVAILABLE" | "LOGIN_FAILED"; message: string } | null = null;
    try {
      const health = await deps.gateway("/health");
      if (health.status !== 200 || !object(health.body)) gatewayProblem = { status: "UNAVAILABLE", message: gatewayCode(health) };
      else if (health.body["terminal_connected"] === false || health.body["ok"] === false)
        gatewayProblem = { status: "LOGIN_FAILED", message: "terminal not connected" };
    } catch (error) {
      gatewayProblem = { status: "UNAVAILABLE", message: `gateway_unreachable: ${errorText(error).slice(0, 200)}` };
    }

    let coverage = new Map<string, number>();
    try {
      coverage = new Map((await deps.store.getFxBarCoverage()).map((item) => [`${item.venue}|${item.instrument_id}`,
        item.newest_open_time_ms]));
    } catch (error) {
      deps.error(`${LOG} coverage read failed: ${errorText(error)}`);
      return result;
    }

    if (!gatewayProblem) await refreshSpecs(deps, state, subs);

    for (const sub of subs) {
      const key = `${sub.venue}|${sub.instrumentId}`;
      const newest = coverage.get(key) ?? null;
      const symbol = symbolOf(sub.instrumentId);
      let outcome: SymbolOutcome | null = null;
      try {
        outcome = gatewayProblem ? null : await collectSymbol(deps, sub, newest, now);
      } catch (error) {
        outcome = { stored: 0, skipped: 0, newestOpen: newest, backfilling: false,
          problem: { status: "UNAVAILABLE", message: `collect failed: ${errorText(error).slice(0, 200)}` } };
        deps.error(`${LOG} ${symbol}: ${errorText(error)}`);
      }
      if (outcome) {
        result.stored[symbol] = outcome.stored;
        result.skipped[symbol] = outcome.skipped;
        if (outcome.skipped) deps.error(`${LOG} ${symbol}: skipped ${outcome.skipped} row(s) that failed the local checks`);
        if (outcome.problem && !outcome.problem.message.startsWith("collect failed"))
          deps.error(`${LOG} ${symbol}: gateway answered ${outcome.problem.message}`);
      }
      result.health.push(fxHealth(sub, now, outcome?.newestOpen ?? newest, outcome, gatewayProblem));
    }
    if (gatewayProblem) deps.error(`${LOG} gateway: ${gatewayProblem.status} ${gatewayProblem.message}`);
    try {
      await deps.store.recordFxCollectorHealth(result.health);
    } catch (error) {
      deps.error(`${LOG} health write failed: ${errorText(error)}`);
    }
  } catch (error) {
    deps.error(`${LOG} pass failed: ${errorText(error)}`);
  }
  return result;
}

// ---------------------------------------------------------------- subscriptions (--subscribe)

const INSTRUMENT_ID = /^(fx|fut):[A-Z0-9._-]{2,20}$/;
const VENUE = /^[a-z0-9-]{3,40}$/;

/** "fx:EURUSD=EURUSD,fx:XAUUSD=XAUUSDm" -> subscriptions (instrument id = native symbol when no "=" part). */
export function parseFxSubscriptions(text: string | undefined, venue: string = FX_DEFAULT_VENUE): FxSubscription[] {
  if (!VENUE.test(venue)) throw new Error("FX_VENUE must match ^[a-z0-9-]{3,40}$");
  const items = (text ?? "").split(",").map((item) => item.trim()).filter(Boolean);
  if (items.length === 0) throw new Error("FX_SUBSCRIPTIONS is empty (for example fx:EURUSD=EURUSD,fx:XAUUSD=XAUUSD)");
  if (items.length > 100) throw new Error("FX_SUBSCRIPTIONS has more than 100 instruments");
  const out: FxSubscription[] = [];
  for (const item of items) {
    const [instrumentId, native = ""] = item.split("=");
    if (!instrumentId || !INSTRUMENT_ID.test(instrumentId)) throw new Error(`bad instrument id ${JSON.stringify(item)}`);
    const nativeSymbol = native || instrumentId.split(":")[1]!;
    if (!/^[A-Za-z0-9._#&-]{1,32}$/.test(nativeSymbol)) throw new Error(`bad native symbol in ${JSON.stringify(item)}`);
    if (out.some((existing) => existing.instrumentId === instrumentId)) throw new Error(`duplicate ${instrumentId}`);
    out.push({ venue, instrumentId, nativeSymbol });
  }
  return out;
}

// ---------------------------------------------------------------- gateway client and scheduler

/** The gateway as `deps.gateway`: bearer token, no redirects, a request timeout, JSON bodies. */
export function createGatewayClient(config: { url: string; token: string }, send: typeof fetch = fetch,
                                    timeoutMs = 30_000): FxCollectorDeps["gateway"] {
  const origin = new URL(config.url);
  if ((origin.protocol !== "http:" && origin.protocol !== "https:") || origin.username || origin.password
      || origin.search || origin.hash || origin.pathname !== "/")
    throw new Error("FX_GATEWAY_URL must be an http(s) origin without credentials, path or query");
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(config.token)) throw new Error("FX_GATEWAY_TOKEN must match ^[A-Za-z0-9_-]{32,256}$");
  return async (path) => {
    const response = await send(new URL(path, origin).toString(), {
      method: "GET", redirect: "manual", signal: AbortSignal.timeout(timeoutMs),
      headers: { Authorization: `Bearer ${config.token}` },
    });
    let body: unknown = null;
    try { body = await response.json(); } catch { /* no JSON body */ }
    return { status: response.status, body };
  };
}

export type FxCollector = { stop(): Promise<void> };

/** Runs a first pass shortly after start, then once per minute at the boundary + 1 s. */
export function startFxCollector(deps: FxCollectorDeps, options: { startDelayMs?: number; settleMs?: number } = {}): FxCollector {
  const state = newFxCollectorState();
  const settle = options.settleMs ?? 1_000;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let running: Promise<unknown> = Promise.resolve();
  const schedule = (delay: number) => {
    if (!stopped) timer = setTimeout(() => { running = tick(); }, delay);
  };
  const tick = async () => {
    await runFxCollectorPass(deps, state);
    const now = deps.now();
    schedule(Math.floor(now / MINUTE_MS) * MINUTE_MS + MINUTE_MS + settle - now);
  };
  schedule(options.startDelayMs ?? 5_000);
  deps.log(`${LOG} started (instance ${deps.instanceId})`);
  return {
    async stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
      await running;
      try { await deps.store.releaseFxCollectorLease(deps.instanceId); }
      catch (error) { deps.error(`${LOG} lease release failed: ${errorText(error)}`); }
    },
  };
}
