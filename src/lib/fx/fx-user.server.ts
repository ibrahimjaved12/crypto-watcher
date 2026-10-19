// Per-user forex pass (#446), the forex twin of the crypto user stage (forward-stages.server.ts runUserStage
// and forward-job.server.ts runHourlyForAccounts). After the shared market stage, every forex-entitled account
// consumes the committed forex stages after its own cursor, in order: the stage's shared events (open, close,
// swap) go to POST /v1/fx/forward/wallet_step with the account's stored state and config, plus the bars and
// contracts the per-user stop-out scan needs, and the answer is committed with commit_fx_user_run (cursor,
// state and ledger move together, or not at all).
//
// Never on partial inputs: a missing spec, bars the store cannot read or a wallet refusal writes a retryable
// `skipped_stale` audit row (market 'forex', run_key 'fx-hour:<boundary>') and the cursor stays. Every
// dependency is injected; the hosted client and the Python call are built by the entrypoint.
import type { FxSymbolSpec, OperationalStore } from "../operational/types";
import { fxContract, fxVenueSymbol, gatewayRow, quoteToAccount } from "./fx-stage.server";

const MINUTE_MS = 60_000;
const LOG = "[forex-users]";
/** One stage's stop-out window never needs more than one get_fx_bars page; a full page is refused, not trimmed. */
export const FX_USER_BAR_LIMIT = 20_000;
/** Stages consumed per account in one pass (a day of hourly stages); the next pass continues. */
export const FX_USER_MAX_STAGES = 48;
const WALLET_CONFIG_KEYS = ["initial_balance_e8", "risk_fraction", "max_positions", "max_exposure_multiple"];

export type FxUserAccount = {
  userId: string;
  accountId: string;
  /** The stored config (defaults are written by ensure_fx_account). */
  config: Record<string, unknown>;
  startBoundary: number;
  /** The last stage boundary this account has consumed (never below its start boundary). */
  cursor: number;
  previousRunId: string | null;
  state: { positions?: Record<string, { symbol?: string }> } | null;
};

export type FxUserStage = {
  id: string;
  boundary_ms: number;
  /** The stage's decision window start (stage.payload.from_ms): it covers (fromMs, boundary_ms]. */
  fromMs: number;
  versions: unknown;
  params_hash: string | null;
  payload: Record<string, unknown>;
  freshness: Record<string, unknown>;
  /** The committed event items, ordered by (ms, symbol, sequence). */
  events: FxStageEvent[];
};

export type FxStageEvent = { type: string; ms: number; symbol?: string; quote_to_account?: string;
  setup?: { symbol?: string; entry_ms?: number } } & Record<string, unknown>;

export type FxWalletResponse = {
  schema_version: number;
  wallet_state: unknown;
  ledger: unknown[];
  wallet_config: unknown;
  assumptions: string[];
};

export type FxUserHosted = {
  /** The user ids entitled to forex (fx_entitled_users). */
  listUsers(): Promise<string[]>;
  /** ensure_fx_account, then the account row and its latest ok forex run. */
  account(userId: string, boundary: number): Promise<FxUserAccount>;
  /** Stages with cursor < boundary_ms <= boundary, oldest first, with their events. */
  stagesAfter(cursor: number, boundary: number): Promise<FxUserStage[]>;
  /** commit_fx_user_run: the run id (the existing one when this stage was already consumed). */
  commit(account: FxUserAccount, stage: FxUserStage, run: Record<string, unknown>, ledger: unknown[]): Promise<string>;
  /** A retryable paper_runs audit row; never moves the cursor. */
  retryable(account: FxUserAccount, boundary: number, status: "skipped_stale", reason: string): Promise<void>;
};

export type FxUserDeps = {
  now(): number;
  store: Pick<OperationalStore, "getFxBars" | "getFxSymbolSpecs">;
  hosted: FxUserHosted;
  callWallet(body: Record<string, unknown>): Promise<FxWalletResponse>;
  log(line: string): void;
  error(line: string): void;
};

export type FxUserSummary = {
  userId: string;
  status: "ok" | "already_done" | "no_stage" | "refused";
  consumed: number;
  reason?: string;
};

/** Inputs the pass could not build honestly; becomes a retryable audit row, never a partial commit. */
class Refusal extends Error {}

// ---------------------------------------------------------------- one stage for one account

type SymbolNeed = { symbol: string; entries: number[] };

/** Symbols the stop-out scan must cover: carried positions and the symbols of the stage's open events. */
function neededSymbols(account: FxUserAccount, events: FxStageEvent[]): Map<string, SymbolNeed> {
  const need = new Map<string, SymbolNeed>();
  const add = (symbol: unknown, entry?: unknown) => {
    if (typeof symbol !== "string" || !symbol) return;
    const item = need.get(symbol) ?? { symbol, entries: [] };
    if (typeof entry === "number") item.entries.push(entry);
    need.set(symbol, item);
  };
  for (const position of Object.values(account.state?.positions ?? {})) add(position.symbol);
  for (const event of events) if (event.type === "open") add(event.setup?.symbol, event.setup?.entry_ms);
  return need;
}

function walletConfig(config: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of WALLET_CONFIG_KEYS) if (config[key] !== undefined) out[key] = config[key];
  return out;
}

async function walletBody(deps: FxUserDeps, account: FxUserAccount, stage: FxUserStage) {
  // An open event before the account existed is not this account's trade (crypto does the same).
  const events = stage.events.filter((event) => event.type !== "open"
    || (event.setup?.entry_ms ?? 0) >= account.startBoundary);
  const need = neededSymbols(account, events);
  const specs = need.size ? await deps.store.getFxSymbolSpecs() : [];
  const contracts: Record<string, ReturnType<typeof fxContract>> = {};
  const symbols = [];
  for (const [symbol, item] of [...need.entries()].sort(([a], [b]) => (a < b ? -1 : 1))) {
    const spec: FxSymbolSpec | undefined = specs.find((candidate) => candidate.instrumentId === `fx:${symbol}`);
    if (!spec) throw new Refusal(`no symbol spec for ${symbol}`);
    contracts[symbol] = fxContract(spec);
    // The conversion rate the stage used for this symbol's events; computed from the feed only when none exists.
    const given = events.find((event) => event.symbol === symbol && typeof event.quote_to_account === "string");
    let conversion = given?.quote_to_account;
    if (conversion === undefined) {
      try { conversion = await quoteToAccount(deps, spec, specs); }
      catch (error) { throw new Refusal(`${symbol}: ${error instanceof Error ? error.message : String(error)}`); }
    }
    // Carried positions are scanned from the minute after the account's cursor, new ones from their entry.
    const fromMs = Math.min(account.cursor + MINUTE_MS, ...item.entries);
    const bars = await deps.store.getFxBars(spec.venue, spec.instrumentId,
      { fromMs, toMs: stage.boundary_ms + MINUTE_MS, limit: FX_USER_BAR_LIMIT });
    if (bars.length >= FX_USER_BAR_LIMIT) throw new Refusal(`${symbol}: more than ${FX_USER_BAR_LIMIT} bars to scan`);
    symbols.push({ symbol, venue_symbol: fxVenueSymbol(spec), quote_to_account: conversion, bars: bars.map(gatewayRow) });
  }
  return { state: account.state, config: walletConfig(account.config), contracts, events, symbols };
}

function validWalletResponse(value: FxWalletResponse): FxWalletResponse {
  const object = (item: unknown) => !!item && typeof item === "object" && !Array.isArray(item);
  if (!object(value) || !object(value.wallet_state) || !Array.isArray(value.ledger) || !object(value.wallet_config)
    || !Array.isArray(value.assumptions) || value.assumptions.some((line) => typeof line !== "string"))
    throw new Refusal("the wallet step returned an invalid response");
  return value;
}

function runPayload(stage: FxUserStage, wallet: FxWalletResponse, walletMs: number) {
  const reasons = (stage.payload["reasons"] ?? {}) as Record<string, unknown>;
  return {
    trigger: "hourly", status: "ok", from_ms: stage.fromMs,
    processed_to_ms: stage.payload["processed_to_ms"] ?? {}, versions: stage.versions, params_hash: stage.params_hash,
    wallet_config: wallet.wallet_config, wallet_state: wallet.wallet_state, assumptions: wallet.assumptions,
    reasons, freshness: { ...stage.freshness, wallet_ms: walletMs },
  };
}

/** One stage, one account: wallet step, then commit. A 40001 ("Wallet changed") is retried once from a fresh read. */
async function consumeStage(deps: FxUserDeps, first: FxUserAccount, stage: FxUserStage, boundary: number) {
  let account = first;
  for (let attempt = 0; ; attempt++) {
    const body = await walletBody(deps, account, stage);
    const started = Date.now();
    let wallet: FxWalletResponse;
    try { wallet = validWalletResponse(await deps.callWallet(body)); }
    catch (error) {
      if (error instanceof Refusal) throw error;
      throw new Refusal(`wallet step refused: ${error instanceof Error ? error.message : String(error)}`);
    }
    try {
      await deps.hosted.commit(account, stage, runPayload(stage, wallet, Date.now() - started), wallet.ledger);
      return;
    } catch (error) {
      if ((error as { code?: string }).code !== "40001" || attempt >= 1) throw error;
      account = await deps.hosted.account(account.userId, boundary);
      if (account.cursor >= stage.boundary_ms) return; // another process consumed it
      deps.log(`${LOG} ${account.userId} wallet changed under stage ${stage.boundary_ms}, retrying once`);
    }
  }
}

/** Consume every committed forex stage after this account's cursor, in order. */
export async function runFxUser(deps: FxUserDeps, userId: string, boundary: number): Promise<FxUserSummary> {
  let account = await deps.hosted.account(userId, boundary);
  const stages = (await deps.hosted.stagesAfter(account.cursor, boundary)).slice(0, FX_USER_MAX_STAGES);
  if (!stages.length)
    return { userId, status: account.cursor >= boundary ? "already_done" : "no_stage", consumed: 0 };
  let consumed = 0;
  for (const stage of stages) {
    try {
      // A stage covers (fromMs, boundary]. If it starts after this account's cursor, decisions in between were
      // never made by any stage, so applying it would silently skip events: stop and wait for the owner.
      if (stage.fromMs > account.cursor)
        throw new Refusal(`gap: stage ${stage.boundary_ms} decides from ${stage.fromMs} but the account is consumed to ${account.cursor}`);
      await consumeStage(deps, account, stage, boundary);
    } catch (error) {
      if (!(error instanceof Refusal)) throw error;
      await deps.hosted.retryable(account, stage.boundary_ms, "skipped_stale", error.message);
      return { userId, status: "refused", consumed, reason: error.message };
    }
    consumed++;
    account = await deps.hosted.account(userId, boundary);
  }
  return { userId, status: "ok", consumed };
}

/** Every entitled account; one failing account never stops the others. */
export async function runFxUsers(deps: FxUserDeps, boundary: number): Promise<{ users: number; failed: number }> {
  const users = await deps.hosted.listUsers();
  if (!users.length) {
    deps.log(`${LOG} no forex-entitled accounts`);
    return { users: 0, failed: 0 };
  }
  let failed = 0;
  for (const userId of users) {
    try {
      const summary = await runFxUser(deps, userId, boundary);
      deps.log(`${LOG} ${userId} fx-hour:${boundary} ${summary.status} stages=${summary.consumed}`
        + (summary.reason ? `: ${summary.reason}` : ""));
      if (summary.status === "refused") failed++;
    } catch (error) {
      failed++;
      deps.error(`${LOG} ${userId}: ${error instanceof Error ? error.message : String(error)}`);
    }
  }
  return { users: users.length, failed };
}

// ---------------------------------------------------------------- hosted client

type Reply = PromiseLike<{ data: unknown; error: { message: string; code?: string } | null }>;
type UserQuery = Reply & {
  select(columns: string): UserQuery;
  eq(column: string, value: unknown): UserQuery;
  gt(column: string, value: unknown): UserQuery;
  lte(column: string, value: unknown): UserQuery;
  order(column: string, options: { ascending: boolean }): UserQuery;
  limit(count: number): UserQuery;
  range(from: number, to: number): UserQuery;
  single(): Reply;
  maybeSingle(): Reply;
  insert(row: Record<string, unknown>): Reply;
};
export type HostedFxUserClient = {
  from(table: "forward_wallet_accounts" | "paper_runs" | "fx_market_stages" | "fx_market_events"): UserQuery;
  rpc(name: "fx_entitled_users" | "ensure_fx_account" | "commit_fx_user_run", args?: Record<string, unknown>): Reply;
};

function check<T>(reply: { data: unknown; error: { message: string; code?: string } | null }, what: string): T {
  if (reply.error) throw Object.assign(new Error(`${what} failed: ${reply.error.message}`),
    reply.error.code ? { code: reply.error.code } : {});
  return reply.data as T;
}

const EVENT_PAGE = 500;

export function createHostedFxUser(client: HostedFxUserClient): FxUserHosted {
  async function eventsOf(stageId: string): Promise<FxStageEvent[]> {
    const out: FxStageEvent[] = [];
    for (let offset = 0; ; offset += EVENT_PAGE) {
      const page = check<{ payload: FxStageEvent }[] | null>(await client.from("fx_market_events").select("payload")
        .eq("stage_run_id", stageId).order("ms", { ascending: true }).order("symbol", { ascending: true })
        .order("sequence", { ascending: true }).range(offset, offset + EVENT_PAGE - 1), "fx_market_events read") ?? [];
      out.push(...page.map((row) => row.payload));
      if (page.length < EVENT_PAGE) return out;
    }
  }
  return {
    async listUsers() {
      const data = check<unknown>(await client.rpc("fx_entitled_users"), "fx_entitled_users");
      if (!Array.isArray(data) || data.some((id) => typeof id !== "string")) throw new Error("fx_entitled_users returned no id list");
      return data as string[];
    },
    async account(userId, boundary) {
      check(await client.rpc("ensure_fx_account", { p_user_id: userId, p_boundary_ms: boundary }), "ensure_fx_account");
      const account = check<{ account_id: string; config: Record<string, unknown>; start_boundary_ms: number | string } | null>(
        await client.from("forward_wallet_accounts").select("*").eq("user_id", userId).eq("market", "forex").single(),
        "forex account read");
      if (!account) throw new Error("Forex wallet account missing after initialization");
      const latest = check<{ id: string; boundary_ms: number | string; consumed_boundary_ms: number | string | null;
        wallet_state: FxUserAccount["state"] } | null>(
        await client.from("paper_runs").select("id,boundary_ms,consumed_boundary_ms,wallet_state")
          .eq("user_id", userId).eq("paper_account", account.account_id).eq("market", "forex").eq("status", "ok")
          .order("boundary_ms", { ascending: false }).order("created_at", { ascending: false })
          .order("id", { ascending: false }).limit(1).maybeSingle(), "forex user cursor read");
      const start = Number(account.start_boundary_ms);
      return { userId, accountId: account.account_id, config: account.config ?? {}, startBoundary: start,
        cursor: Math.max(start, Number(latest?.consumed_boundary_ms ?? latest?.boundary_ms ?? 0)),
        previousRunId: latest?.id ?? null, state: latest?.wallet_state ?? null };
    },
    async stagesAfter(cursor, boundary) {
      const rows = check<{ id: string; boundary_ms: number | string; versions: unknown; params_hash: string | null;
        payload: Record<string, unknown>; freshness: Record<string, unknown> }[] | null>(
        await client.from("fx_market_stages").select("id,boundary_ms,versions,params_hash,payload,freshness")
          .gt("boundary_ms", cursor).lte("boundary_ms", boundary).order("boundary_ms", { ascending: true })
          .limit(FX_USER_MAX_STAGES), "fx_market_stages read") ?? [];
      const stages: FxUserStage[] = [];
      for (const row of rows) {
        const fromMs = Number(row.payload?.["from_ms"]);
        if (!Number.isSafeInteger(fromMs)) throw new Error(`stage ${row.id} has no from_ms`);
        stages.push({ id: row.id, boundary_ms: Number(row.boundary_ms), fromMs, versions: row.versions,
          params_hash: row.params_hash, payload: row.payload, freshness: row.freshness ?? {},
          events: await eventsOf(row.id) });
      }
      return stages;
    },
    async commit(account, stage, run, ledger) {
      const id = check<unknown>(await client.rpc("commit_fx_user_run", {
        p_user_id: account.userId, p_stage_run_id: stage.id, p_run: run, p_ledger: ledger,
        p_expected_run_id: account.previousRunId,
      }), "commit_fx_user_run");
      if (typeof id !== "string") throw new Error("commit_fx_user_run returned no run id");
      return id;
    },
    async retryable(account, boundary, status, reason) {
      check(await client.from("paper_runs").insert({
        user_id: account.userId, paper_account: account.accountId, market: "forex", run_key: `fx-hour:${boundary}`,
        boundary_ms: boundary, status, trigger: "hourly", reason: reason.slice(0, 2000),
      }), "forex retry audit");
    },
  };
}

// ---------------------------------------------------------------- Python call

export function createFxWalletCall(config: { url: string; token: string }, send: typeof fetch = fetch,
                                   timeoutMs = 60_000): FxUserDeps["callWallet"] {
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
      throw new Error(`Forex wallet step failed with HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
    }
    return (await response.json()) as FxWalletResponse;
  };
}
