import type { SupabaseClient } from "@supabase/supabase-js";
import {
  FINAL_STATUSES,
  SIGMA_VERSION,
  type ForwardEvaluateResponse,
  type ForwardResolution,
  type WalletState,
} from "./forward-contract";
import { newOutcomeRows, type ForwardRepository, type StoredOpenSetup } from "./forward-run.server";

/**
 * Application-DB persistence of forward records (#239 P11), service role, account-scoped.
 * Inserts only; duplicates are ignored on their natural keys, so a re-run is a no-op.
 */
export const PAPER_ACCOUNT = "default";

function fail(error: { message: string } | null, operation: string): void {
  if (error) throw new Error(`Forward ${operation} failed: ${error.message}`);
}

export function createForwardRepository(client: SupabaseClient): ForwardRepository {
  async function insert(table: string, rows: Record<string, unknown>[], conflict: string) {
    for (let i = 0; i < rows.length; i += 500) {
      const { error } = await client.from(table).upsert(rows.slice(i, i + 500),
        { onConflict: conflict, ignoreDuplicates: true });
      fail(error, `${table} insert`);
    }
    return rows.length;
  }

  return {
    async loadSigmaStates(symbols) {
      const { data, error } = await client.from("forward_sigma_state")
        .select("symbol,sigma_version,as_of_ms,payload,checksum").eq("sigma_version", SIGMA_VERSION).in("symbol", [...symbols]);
      fail(error, "sigma state read");
      return Object.fromEntries((data ?? []).map((row: { symbol: string }) => [row.symbol, row]));
    },
    async commitRun(userId, row, response, previous, candleVersions, states, expected, expectedRunId) {
      const version = (id: string) => ({ candle_version: candleVersions.get(id) ?? null });
      const records = {
        signals: response.signals.map((signal) => ({ ...signal, ...version(signal.signal_id) })),
        setups: response.setups.map((setup) => ({
          setup_id: setup.setup_id, signal_id: setup.signal_id, strategy_id: setup.strategy_id,
          version: setup.version, symbol: setup.symbol, side: setup.side, horizon_min: setup.horizon_min,
          signal_ms: setup.signal_ms, entry_ms: setup.entry_ms, k: setup.k, rr: setup.rr, status: setup.status,
          params_hash: setup.params_hash, ...version(setup.signal_id), payload: setup,
        })),
        outcomes: newOutcomeRows(response, previous),
        ledger: response.ledger.map((line) => ({
          paper_account: PAPER_ACCOUNT, seq: line.seq, ms: line.ms, type: line.type,
          setup_id: line.setup_id ?? null, symbol: line.symbol ?? null, amount_e8: line.amount_e8,
          balance_e8: line.balance_e8, payload: line,
        })),
      };
      const { data, error } = await client.rpc("commit_forward_sigma_run", {
        p_user_id: userId, p_run: row, p_records: records, p_states: states,
        p_expected: expected, p_expected_run_id: expectedRunId,
      });
      fail(error, "atomic run and sigma commit");
      return data as Record<string, number>;
    },
    async findRun(userId, runKey) {
      const { data, error } = await client.from("paper_runs").select("id, status")
        .eq("user_id", userId).eq("paper_account", PAPER_ACCOUNT).eq("run_key", runKey).in("status", ["ok", "failed"]).maybeSingle();
      fail(error, "run read");
      return (data as { id: string; status: string } | null) ?? null;
    },
    async latestOkRun(userId) {
      const { data, error } = await client.from("paper_runs").select("id, processed_to_ms, wallet_state")
        .eq("user_id", userId).eq("paper_account", PAPER_ACCOUNT).eq("status", "ok")
        .order("boundary_ms", { ascending: false }).limit(1).maybeSingle();
      fail(error, "latest run read");
      if (!data) return null;
      const row = data as { id: string; processed_to_ms: Record<string, number>; wallet_state: unknown };
      return { id: row.id, processedToMs: row.processed_to_ms ?? {}, walletState: (row.wallet_state as WalletState | null) ?? null };
    },
    async openSetups(userId) {
      const { data: setups, error } = await client.from("forward_setups").select("setup_id, payload")
        .eq("user_id", userId).eq("status", "T").order("entry_ms", { ascending: false }).limit(2000);
      fail(error, "setup read");
      const ids = (setups ?? []).map((row: { setup_id: string }) => row.setup_id);
      if (!ids.length) return [];
      const outcomes: unknown[] = [];
      for (let i = 0; i < ids.length; i += 100) {  // bounded URL length per request
        const { data, error: outcomeError } = await client.from("forward_outcomes")
          .select("setup_id, status, payload, created_at").eq("user_id", userId).in("setup_id", ids.slice(i, i + 100));
        fail(outcomeError, "outcome read");
        outcomes.push(...(data ?? []));
      }
      const latest = new Map<string, { status: string; payload: ForwardResolution; created_at: string }>();
      for (const row of outcomes as { setup_id: string; status: string; payload: ForwardResolution;
        created_at: string }[]) {
        const current = latest.get(row.setup_id);
        if (FINAL_STATUSES.has(row.status) || !current || (!FINAL_STATUSES.has(current.status)
            && row.created_at > current.created_at)) latest.set(row.setup_id, row);
      }
      const open: StoredOpenSetup[] = [];
      for (const row of (setups ?? []) as { setup_id: string; payload: Record<string, unknown> }[]) {
        const outcome = latest.get(row.setup_id);
        if (outcome && FINAL_STATUSES.has(outcome.status)) continue;
        open.push({ setup: row.payload, resolution: outcome?.payload ?? null });
      }
      return open;
    },
    async insertRun(userId, row) {
      const { data, error } = await client.from("paper_runs")
        .insert({ ...row, user_id: userId, paper_account: PAPER_ACCOUNT }).select("id").single();
      fail(error, "run insert");
      return (data as { id: string }).id;
    },
    async persistEvaluation(userId, runId, response: ForwardEvaluateResponse, previous, candleVersions) {
      const owned = <T extends Record<string, unknown>>(rows: T[]) =>
        rows.map((row) => ({ ...row, user_id: userId, run_id: runId }));
      // P15: the decision candle version is a column only (the setup payload goes back to Python).
      const version = (signalId: string) =>
        candleVersions ? { candle_version: candleVersions.get(signalId) ?? null } : {};
      const signals = await insert("forward_signals", owned(response.signals.map((signal) => ({
        ...signal, ...version(signal.signal_id) }))), "user_id,signal_id");
      const setups = await insert("forward_setups", owned(response.setups.map((setup) => ({
        setup_id: setup.setup_id, signal_id: setup.signal_id, strategy_id: setup.strategy_id,
        version: setup.version, symbol: setup.symbol, side: setup.side, horizon_min: setup.horizon_min,
        signal_ms: setup.signal_ms, entry_ms: setup.entry_ms, k: setup.k, rr: setup.rr, status: setup.status,
        params_hash: setup.params_hash, ...version(setup.signal_id), payload: setup }))),
        "user_id,setup_id");
      const outcomes = await insert("forward_outcomes", owned(newOutcomeRows(response, previous)),
        "user_id,setup_id,status");
      const ledger = await insert("paper_ledger", owned(response.ledger.map((line) => ({
        paper_account: PAPER_ACCOUNT, seq: line.seq, ms: line.ms, type: line.type,
        setup_id: line.setup_id ?? null, symbol: line.symbol ?? null, amount_e8: line.amount_e8,
        balance_e8: line.balance_e8, payload: line }))), "user_id,paper_account,seq");
      return { signals, setups, outcomes, ledger };
    },
  };
}
