import type { ForwardMinuteRow } from "../operational/types";
import type { HostedTraffic } from "./forward-hosted-traffic.server";
import type { LoadedSigma, HostedCheckpoint } from "./forward-sigma-cache.server";
import { forwardMarketResponseSchema, validateSigmaState, sigmaCanonical, validateForwardResponse,
  type ForwardEvaluateResponse, type SigmaStateRecord, type WalletState } from "./forward-contract";
import { FORWARD_SYMBOLS, FORWARD_HISTORY_DAYS, FORWARD_MAX_CATCHUP_CHUNKS, HOUR_MS, repairSymbolHistory, staleness,
  missingMinutes, fetchRunFunding, rebuildSigmaState, decisionCandleVersions,
  type ForwardDeps, type ForwardRunInput, type ForwardRunSummary, type StoredOpenSetup } from "./forward-run.server";

const MINUTE_MS = 60_000, DAY_MS = 86_400_000;
type MarketResponse = Omit<ForwardEvaluateResponse, "wallet_state" | "wallet_config" | "ledger">;
type MarketPart = ReturnType<typeof forwardMarketResponseSchema.parse>;
export type MarketStage = { id: string; boundary_ms: number; response: MarketResponse;
  events: MarketPart["events"]; adverse_paths: MarketPart["adverse_paths"]; freshness: Record<string, unknown> };
export type UserAccount = { userId: string; accountId: string; cursor: number; startBoundary: number; previousRunId: string | null;
  state: WalletState | null; config: Record<string, unknown> };
export type MarketCursor = { id: string; boundary_ms: number; processed_to_ms: Record<string, number> };
export interface SharedRepository {
  traffic?: HostedTraffic;
  stageId(boundary: number): Promise<string | null>;
  stage(boundary: number): Promise<MarketStage | null>;
  latestStage(): Promise<MarketCursor | null>;
  openSetups(): Promise<StoredOpenSetup[]>;
  commitMarket(stage: MarketStage, previous: Map<string, string | null>, candleVersions: Map<string, string | null>): Promise<string>;
  account(userId: string, boundary: number): Promise<UserAccount>;
  stagesAfter(cursor: number, boundary: number, current?: MarketStage[]): Promise<MarketStage[]>;
  commitUser(account: UserAccount, stage: MarketStage, response: ForwardEvaluateResponse, trigger: string): Promise<void>;
  retryable(userId: string, boundary: number, status: string, reason: string): Promise<void>;
}

export type PendingCheckpoints = Map<string, { state: SigmaStateRecord | null; hosted: HostedCheckpoint | null }>;
function clampRange(deps: ForwardDeps, start: number, boundary: number): number {
  const clamped = Math.max(start, boundary - FORWARD_HISTORY_DAYS * DAY_MS);
  if (clamped !== start) deps.log?.(`[forward-range] clamped from=${new Date(start).toISOString()} to=${new Date(clamped).toISOString()}`);
  return clamped;
}

/** Market-only inputs; no account reads, wallet config, or wallet calls. */
export async function runMarketStage(deps: ForwardDeps, input: Omit<ForwardRunInput, "userId">,
  boundaryMs = Math.floor(deps.now() / HOUR_MS) * HOUR_MS,
  firstBoundaryMs = boundaryMs - HOUR_MS, pendingCheckpoints: PendingCheckpoints = new Map(),
  rangeBoundaryMs = boundaryMs, priorClampedHours = 0): Promise<MarketStage | ForwardRunSummary> {
  const repository = deps.repository.shared!;
  const existingId = await repository.stageId(boundaryMs);
  if (existingId) {
    const existing = await repository.stage(boundaryMs);
    if (!existing || existing.id !== existingId) throw new Error("Existing market stage missing");
    return existing; // Before loading sigma: another user cannot rebuild this hour.
  }
  const symbols = input.symbols ?? FORWARD_SYMBOLS;
  const runKey = `hour:${boundaryMs}`;
  const stageRunId = crypto.randomUUID();
  if (!deps.callMarket || !deps.sigmaCache || !deps.repository.restoreSigmaState ||
      !deps.repository.checkpointSigmaState || !deps.firstMinute)
    throw new Error("Incomplete shared market dependencies");
  const loaded: Record<string, LoadedSigma> = {};
  const states: Record<string, SigmaStateRecord | null> = {};
  const rebuildReasons: string[] = [];
  for (const symbol of symbols) {
    const entry = await deps.sigmaCache.load(symbol, () => deps.repository.restoreSigmaState!(symbol));
    loaded[symbol] = entry;
    if (!pendingCheckpoints.has(symbol)) pendingCheckpoints.set(symbol, { state: null,
      hosted: entry.hosted ? { ...entry.hosted } : null });
    states[symbol] = entry.state;
  }
  const sinceMs = boundaryMs - FORWARD_HISTORY_DAYS * DAY_MS;
  const rows = new Map<string, ForwardMinuteRow[]>();
  const backfillErrors: string[] = [];
  for (const symbol of symbols) {
    const readFrom = states[symbol] ? Math.min(sinceMs, states[symbol]!.as_of_ms + MINUTE_MS) : sinceMs;
    const repaired = await repairSymbolHistory(deps, symbol, readFrom, boundaryMs, input.trigger === "hourly");
    if (repaired.error) backfillErrors.push(repaired.error);
    const series = repaired.series;
    rows.set(symbol, series);
  }
  const backfillReason = backfillErrors.length ? `backfill_failed: ${backfillErrors.join(", ")}` : null;
  const health = await deps.listHealth([...symbols]);
  const freshness: Record<string, unknown> = Object.fromEntries(
    symbols.map((symbol) => {
      const series = rows.get(symbol) ?? [];
      return [symbol, { rows: series.length, last_open_ms: series[series.length - 1]?.open_time_ms ?? null,
        missing_minutes: missingMinutes(series, sinceMs, boundaryMs),
        ...(states[symbol] ? { sigma_source: states[symbol]!.payload.source,
          sigma_payload_bytes: new TextEncoder().encode(sigmaCanonical(states[symbol]!.payload)).byteLength } : {}),
      }];
    }),
  );
  const stale = staleness(symbols, rows, health, boundaryMs);
  if (stale.length) {
    const reason = [...stale, ...(backfillReason ? [backfillReason] : [])].join("; ");

    return { runKey, status: "skipped_stale", reason };
  }

  const latest = await repository.latestStage();
  const originalFirst = firstBoundaryMs;
  firstBoundaryMs = clampRange(deps, firstBoundaryMs, rangeBoundaryMs);
  const originalPrevious = latest ? Math.min(...symbols.map((s) => latest.processed_to_ms[s] ?? boundaryMs - HOUR_MS))
    : firstBoundaryMs;
  const previousTo = clampRange(deps, originalPrevious, rangeBoundaryMs);
  freshness["range_clamped_hours"] = Math.max(priorClampedHours,
    (firstBoundaryMs - originalFirst) / HOUR_MS, (previousTo - originalPrevious) / HOUR_MS);
  const fromMs = Math.min(previousTo, boundaryMs - MINUTE_MS);
  const open = await repository.openSetups();
  const oldestEntry = Math.min(fromMs, ...open.map((item) => Number(item.setup["entry_ms"])).filter(Number.isFinite));
  const funding = await fetchRunFunding(deps, symbols, oldestEntry - HOUR_MS);
  const fundingUnavailable = symbols.filter((symbol) => funding.get(symbol) === null);
  // Indicator lookback rows and Python latency (sigma checkpoints do not replace TA warm-up).
  const requestRows = symbols.reduce((sum, symbol) => sum + (rows.get(symbol)?.length ?? 0), 0);
  const pythonStarted = Date.now();
  let response: MarketResponse;
  let marketEvents: MarketStage["events"] = [];
  let adversePaths: MarketStage["adverse_paths"] = [];
  const advancedStates: SigmaStateRecord[] = [];
  {

    const parts: ReturnType<typeof forwardMarketResponseSchema.parse>[] = [];
    const events: MarketStage["events"] = [];
    for (const symbol of symbols) {
      let state = states[symbol];
      const rebuild = async (why: string) => {
        const reason = `sigma_rebuild: ${symbol} ${why}`;
        rebuildReasons.push(reason);
        deps.log?.(reason);
        loaded[symbol]!.source = "rebuilt";
        // Rebuild only from authoritative stored minutes, never from a different sigma model.
        state = await rebuildSigmaState(deps, symbol, fromMs, input.strategyIds);
      };
      if (!state) await rebuild("missing state after local/hosted restore");
      else if (state.as_of_ms >= boundaryMs || fromMs < state.as_of_ms - 2 * DAY_MS)
        await rebuild("decision outside checkpoint cache");
      const request = () => ({
        schema_version: 1, symbol,
        rows: (rows.get(symbol) ?? []).map(({ candle_hash: _hash, ...row }) => row),
        funding: funding.get(symbol) ?? [], funding_available: funding.get(symbol) !== null,
        strategy_ids: input.strategyIds, from_ms: fromMs, to_ms: boundaryMs - MINUTE_MS,
        open_setups: open.filter((item) => item.setup["symbol"] === symbol),
        stage_run_id: stageRunId, use_sigma_state: true, sigma_state: state,
      });
      let value;
      try { value = await deps.callMarket!(request()); }
      catch (error) {
        if (!(error instanceof Error) || error.message !== "sigma_state_rebuild_required") throw error;
        await rebuild("Python state identity mismatch");
        value = await deps.callMarket!(request());
      }
      const part = forwardMarketResponseSchema.parse(value);
      if (part.symbol !== symbol) throw new Error("Market symbol mismatch");
      if (part.events.some((event) => event.stage_run_id !== stageRunId || event.symbol !== symbol) ||
          part.adverse_paths.some((path) => path.symbol !== symbol))
        throw new Error("Market event/path identity mismatch");
      const priorPart = parts[0];
      if (priorPart && (part.params_hash !== priorPart.params_hash ||
          sigmaCanonical(part.versions) !== sigmaCanonical(priorPart.versions)))
        throw new Error("Market stage version mismatch");
      const advanced = await validateSigmaState(part.sigma_state, symbol);
      if (advanced.as_of_ms !== boundaryMs - MINUTE_MS) throw new Error("Sigma state boundary mismatch");
      const payloadBytes = new TextEncoder().encode(sigmaCanonical(advanced.payload)).byteLength;
      freshness[symbol] = { ...(freshness[symbol] as Record<string, unknown>),
        sigma_source: advanced.payload.source, sigma_payload_bytes: payloadBytes };
      deps.log?.(`[forward-sigma] ${symbol} source=${loaded[symbol]!.source}` +
        ` as_of=${new Date(advanced.as_of_ms).toISOString()} checksum=${advanced.checksum.slice(0, 8)} bytes=${payloadBytes}`);
      advancedStates.push(advanced);
      for (const event of [...part.events].sort((a, b) => a.sequence - b.sequence))
        events.push(event);
      parts.push(part);
    }
    // Funding is settled once in its own stage; history rows remain available to outcomes.
    marketEvents = events.filter((event) => event.type !== "funding" || event.ms > fromMs);
    adversePaths = parts.flatMap((part) => part.adverse_paths);
    const first = parts[0];
    if (!first) throw new Error("No forward symbols");
    response = {
      schema_version: 1, versions: first.versions, params_hash: first.params_hash,
      assumptions: first.assumptions,
      processed_to_ms: Object.fromEntries(parts.map((p) => [p.symbol, p.processed_to_ms])),
      funding_unavailable: parts.filter((p) => !p.funding_available).map((p) => p.symbol),
      signals: parts.flatMap((p) => p.signals), setups: parts.flatMap((p) => p.setups),
      resolutions: parts.flatMap((p) => p.resolutions), reasons: Object.fromEntries(parts.map((p) => [p.symbol, p.reasons])),
    };
  }
  freshness["request"] = { rows: requestRows, python_ms: Date.now() - pythonStarted };
  const noSigma = symbols.length > 0 && symbols.every((symbol) =>
    String(response.reasons[symbol]?.["sigma"] ?? "").startsWith("no_sigma")) &&
    response.setups.length === 0 && response.resolutions.length === 0;
  const reason = [...rebuildReasons, ...(backfillReason ? [backfillReason] : []),
    ...(fundingUnavailable.length ? [`funding_unavailable: ${fundingUnavailable.join(",")}`] : [])].join("; ");
  if (noSigma) return { runKey, status: "no_sigma", reason };
  const stage: MarketStage = { id: stageRunId, boundary_ms: boundaryMs, response,
    events: marketEvents, adverse_paths: adversePaths, freshness };
  const previous = new Map(open.map((item): [string, string | null] =>
    [String(item.setup["setup_id"]), item.resolution?.status ?? null]));
  const committedId = await repository.commitMarket(stage, previous, await decisionCandleVersions(response, rows));
  if (committedId !== stage.id) {
    // Another process won this boundary; never save its losing calculation's sigma.
    const committed = await repository.stage(boundaryMs);
    if (!committed || committed.id !== committedId) throw new Error("Committed market stage missing");
    return committed;
  }
  for (const state of advancedStates) {
    const entry = loaded[state.symbol]!;
    try {
      if (!await deps.sigmaCache.save({ state, hosted: entry.hosted })) continue;
    } catch (error) {
      deps.log?.(`[forward-sigma] cache ${state.symbol} failed (${String(error)})`);
      continue; // The committed stage and its account replays remain usable.
    }
    pendingCheckpoints.set(state.symbol, { state,
      hosted: pendingCheckpoints.has(state.symbol) ? pendingCheckpoints.get(state.symbol)!.hosted : entry.hosted });
  }
  return stage;
}

export async function checkpointDueSigma(deps: ForwardDeps, pending: PendingCheckpoints): Promise<void> {
  for (const entry of pending.values()) {
    const { state } = entry;
    if (!state) continue;
    if (entry.hosted?.checkpointed_at != null && deps.now() - entry.hosted.checkpointed_at < DAY_MS) continue;
    const started = Date.now();
    try {
      await deps.repository.checkpointSigmaState!(state, entry.hosted?.checksum ?? null);
      await deps.sigmaCache!.checkpointed(state, deps.now());
      deps.log?.(`[forward-sigma] checkpoint ${state.symbol} written ms=${Date.now() - started}`);
    } catch (error) {
      deps.log?.(`[forward-sigma] checkpoint ${state.symbol} failed (${String(error)}) ms=${Date.now() - started}`);
      if ((error as { code?: string }).code === "40001") {
        try { await deps.sigmaCache!.conflict(state.symbol); }
        catch (markError) { deps.log?.(`[forward-sigma] restore marker ${state.symbol} failed (${String(markError)})`); }
      }
    }
  }
}

/** Compute bounded chunks before replaying accounts, reloading committed market facts each time. */
export async function runMarketCatchup(deps: ForwardDeps, input: Omit<ForwardRunInput, "userId">,
  boundary: number, firstBoundary: number, pending: PendingCheckpoints) {
  const first = clampRange(deps, firstBoundary, boundary);
  const stages: MarketStage[] = [];
  let stop: ForwardRunSummary | undefined;
  let processed = first;
  for (let chunk = 0; chunk < FORWARD_MAX_CATCHUP_CHUNKS; chunk++) {
    const latest = await deps.repository.shared!.latestStage();
    const anchor = latest ? clampRange(deps, latest.boundary_ms, boundary) : Math.floor(first / HOUR_MS) * HOUR_MS;
    const end = Math.min(boundary, anchor + DAY_MS);
    const stage = await runMarketStage(deps, input, end, first, pending, boundary,
      chunk === 0 ? (first - firstBoundary) / HOUR_MS : 0);
    if (!("id" in stage)) { stop = stage; processed = stages.at(-1)?.boundary_ms ?? anchor; break; }
    stages.push(stage);
    processed = end;
    if (end >= boundary) break;
  }
  deps.log?.(`[forward-catchup] chunks=${stages.length} remaining_hours=${Math.max(0, (boundary - processed) / HOUR_MS)}`);
  return { stages, boundary: processed, stop };
}

/** Wallet-only inputs: reads shared stages after this account's cursor in stage order. */
export async function runUserStage(deps: ForwardDeps, account: UserAccount, boundary: number,
  trigger: ForwardRunInput["trigger"], currentStage?: MarketStage[]): Promise<ForwardRunSummary> {
  const repository = deps.repository.shared!;
  const stages = await repository.stagesAfter(account.cursor, boundary, currentStage);
  if (!stages.length) return { runKey: `hour:${boundary}`, status: "already_done" };
  let current = account;
  for (const stage of stages) {
    const { version: _version, assumptions: _assumptions, ...config } = current.config;
    const events = stage.events.filter((event) => event.type !== "open" ||
      event.setup.entry_ms >= current.startBoundary);
    const wallet = await deps.callWallet!({ schema_version: 1, state: current.state, config,
      events, adverse_paths: stage.adverse_paths }) as
      { wallet_state: unknown; ledger: unknown; wallet_config: unknown; assumptions: string[] };
    const response = validateForwardResponse({ ...stage.response, wallet_state: wallet.wallet_state,
      ledger: wallet.ledger, wallet_config: wallet.wallet_config,
      assumptions: [...stage.response.assumptions, ...wallet.assumptions] });
    await repository.commitUser(current, stage, response, trigger);
    current = await repository.account(account.userId, boundary);
  }
  return { runKey: `hour:${boundary}`, status: "ok" };
}

export async function runSharedForward(deps: ForwardDeps, input: ForwardRunInput): Promise<ForwardRunSummary> {
  const traffic = deps.repository.shared!.traffic;
  traffic?.reset();
  try {
    const boundary = Math.floor(deps.now() / HOUR_MS) * HOUR_MS;
    const account = await deps.repository.shared!.account(input.userId, boundary);
    const pending: PendingCheckpoints = new Map();
    const result = await runMarketCatchup(deps, input, boundary, account.startBoundary, pending);
    try {
      const summary = result.stages.length
        ? await runUserStage(deps, account, result.boundary, input.trigger, result.stages) : result.stop!;
      if (result.stop) {
        await deps.repository.shared!.retryable(input.userId, Number(result.stop.runKey.split(":")[1]), result.stop.status, result.stop.reason ?? "");
        return result.stop;
      }
      return summary;
    } finally {
      try { await checkpointDueSigma(deps, pending); }
      catch (error) { deps.log?.(`[forward-sigma] checkpoints failed (${String(error)})`); }
    }
  } finally { if (traffic) deps.log?.(traffic.line()); }
}
