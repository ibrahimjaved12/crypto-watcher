import type { SupabaseClient } from "@supabase/supabase-js";

import type { LedgerRow, Quote, RunInfo, SetupMeta, SummaryRaw } from "./paper-account";

/**
 * Reads for the Paper account page (#36 Part 3): the signed-in user's wallet rows through RLS, nothing else.
 * Totals come from one SQL function (every ledger line, no row cap); lists are keyset-paginated by ledger `seq`
 * (newest first) like the Strategy Lab logs. Rows are returned as stored: the page maps them in the browser.
 */
export const TRADES_PAGE = 20;
export const REJECTED_PAGE = 50;
export const LEDGER_PAGE = 50;
const SETUP_CHUNK = 20;
const METADATA_CHUNK = 100;
const HOUR_MS = 3_600_000;
const DAY_MS = 86_400_000;
const DAILY_EQUITY_SPAN_MS = 60 * DAY_MS;
const SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT"];
const MONEY_KINDS = ["open_fee", "pnl", "close_fee", "funding", "liquidation"];
const LEDGER_COLUMNS = "seq, ms, type, setup_id, symbol, amount_e8, balance_e8, payload";
const CURSOR = /^\d{1,18}$/;

type Result = { data: unknown; error: { message: string } | null };
type Query = PromiseLike<Result> & {
  select(columns: string): Query;
  eq(column: string, value: unknown): Query;
  in(column: string, values: readonly unknown[]): Query;
  lt(column: string, value: unknown): Query;
  or(filter: string): Query;
  order(column: string, options: { ascending: boolean }): Query;
  limit(count: number): Query;
};

export type Page = {
  lines: LedgerRow[];
  strategies: Record<string, string>;
  nextCursor: string | null;
};
export type PaperAccountData = {
  accountId: string | null;
  run: RunInfo | null;
  summary: SummaryRaw | null;
  summaryError: string | null;
  equity: { ms: number; balance_e8: number }[];
  equityBucketMs: number;
  quotes: Quote[];
  /** Strategy and horizon of each open position's setup. */
  positionMeta: Record<string, SetupMeta>;
};

export const isValidCursor = (cursor: string) => CURSOR.test(cursor);

async function rows<T>(query: PromiseLike<Result>, what: string): Promise<T[]> {
  const { data, error } = await query;
  if (error) throw new Error(`Paper account ${what} read failed: ${error.message}`);
  return (data ?? []) as T[];
}

const num = (value: unknown): number => Number(value ?? 0);

/** SQL bigint/numeric arrive as numbers or decimal text; the totals fit a double (|e8| below 2^53). */
export function toSummary(row: Record<string, unknown>): SummaryRaw {
  return {
    trades: num(row["trades"]),
    target: num(row["target"]),
    stopped: num(row["stopped"]),
    expired: num(row["expired"]),
    liquidated: num(row["liquidated"]),
    ambiguous: num(row["ambiguous"]),
    unresolved: num(row["unresolved"]),
    wins: num(row["wins"]),
    losses: num(row["losses"]),
    sum_net_e8: num(row["sum_net_e8"]),
    gross_wins_e8: num(row["gross_wins_e8"]),
    gross_losses_e8: num(row["gross_losses_e8"]),
    rejected: num(row["rejected"]),
    order_rejected: num(row["order_rejected"]),
    open_fees_e8: num(row["open_fees_e8"]),
    close_fees_e8: num(row["close_fees_e8"]),
    funding_e8: num(row["funding_e8"]),
    liquidation_e8: num(row["liquidation_e8"]),
    pnl_e8: num(row["pnl_e8"]),
    lines: num(row["lines"]),
    first_ms:
      row["first_ms"] === null || row["first_ms"] === undefined ? null : num(row["first_ms"]),
    last_ms: row["last_ms"] === null || row["last_ms"] === undefined ? null : num(row["last_ms"]),
  };
}

/** Latest wallet run, the totals, the balance history and the published prices, in two round trips. */
export async function loadPaperAccount(client: SupabaseClient): Promise<PaperAccountData> {
  const from = (table: string) => client.from(table) as unknown as Query;
  const [runs, quotes] = await Promise.all([
    rows<RunInfo & { paper_account: string }>(
      from("paper_runs")
        .select("boundary_ms, created_at, paper_account, wallet_config, wallet_state")
        .eq("status", "ok")
        .order("boundary_ms", { ascending: false })
        .order("created_at", { ascending: false })
        .limit(1),
      "wallet",
    ),
    rows<Quote>(
      from("market_quotes").select("symbol, price, published_at").in("symbol", SYMBOLS),
      "prices",
    ).catch(() => []),
  ]);
  const run = runs[0] ?? null;
  const positionMeta = await setupMeta(client, Object.keys(run?.wallet_state?.positions ?? {}));
  let summary: SummaryRaw | null = null;
  let summaryError: string | null = null;
  const { data, error } = await (client.rpc(
    "paper_account_summary",
  ) as unknown as PromiseLike<Result>);
  if (error) summaryError = error.message;
  else summary = toSummary(((data as Record<string, unknown>[] | null) ?? [])[0] ?? {});
  const span =
    summary && summary.first_ms !== null && summary.last_ms !== null
      ? summary.last_ms - summary.first_ms
      : 0;
  const bucketMs = span > DAILY_EQUITY_SPAN_MS ? DAY_MS : HOUR_MS;
  const equity =
    summary && summary.lines > 0
      ? await rows<{ ms: number | string; balance_e8: number | string }>(
          client.rpc("paper_equity_series", {
            p_bucket_ms: bucketMs,
            p_from_ms: null,
          }) as unknown as PromiseLike<Result>,
          "balance history",
        )
      : [];
  return {
    accountId: run?.paper_account ?? null,
    run: run
      ? {
          boundary_ms: Number(run.boundary_ms),
          created_at: run.created_at,
          wallet_config: run.wallet_config,
          wallet_state: run.wallet_state,
        }
      : null,
    summary,
    summaryError,
    equity: equity.map((point) => ({ ms: Number(point.ms), balance_e8: Number(point.balance_e8) })),
    equityBucketMs: bucketMs,
    quotes: quotes.map((quote) => ({
      symbol: quote.symbol,
      price: Number(quote.price),
      published_at: quote.published_at,
    })),
    positionMeta,
  };
}

async function setupMeta(
  client: SupabaseClient,
  setupIds: readonly string[],
): Promise<Record<string, SetupMeta>> {
  const out: Record<string, SetupMeta> = {};
  for (let offset = 0; offset < setupIds.length; offset += METADATA_CHUNK) {
    const setups = await rows<{
      setup_id: string;
      strategy_id: string;
      horizon_min: number | null;
    }>(
      (client.from("forward_market_setups") as unknown as Query)
        .select("setup_id, strategy_id, horizon_min")
        .in("setup_id", setupIds.slice(offset, offset + METADATA_CHUNK)),
      "position strategies",
    );
    for (const setup of setups)
      out[setup.setup_id] = { strategy_id: setup.strategy_id, horizon_min: setup.horizon_min };
  }
  return out;
}

async function strategiesOf(
  client: SupabaseClient,
  setupIds: readonly string[],
): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  const ids = [...new Set(setupIds.filter(Boolean))];
  for (let offset = 0; offset < ids.length; offset += METADATA_CHUNK) {
    const setups = await rows<{ setup_id: string; strategy_id: string }>(
      (client.from("forward_market_setups") as unknown as Query)
        .select("setup_id, strategy_id")
        .in("setup_id", ids.slice(offset, offset + METADATA_CHUNK)),
      "strategies",
    );
    for (const setup of setups) out[setup.setup_id] = setup.strategy_id;
  }
  return out;
}

function ledger(client: SupabaseClient, accountId: string): Query {
  return (client.from("paper_ledger") as unknown as Query)
    .select(LEDGER_COLUMNS)
    .eq("paper_account", accountId);
}

function pageOf(
  found: LedgerRow[],
  size: number,
): { page: LedgerRow[]; nextCursor: string | null } {
  const more = found.length > size;
  const page = more ? found.slice(0, size) : found;
  const last = page[page.length - 1];
  return { page, nextCursor: more && last ? String(last.seq) : null };
}

/**
 * One page of closed trades: the closing lines (a liquidation, or a result that is not a partial reduction) newest
 * first, then every money line of those setups, so a trade's fees and funding are complete whatever page its
 * opening fee would have been on.
 */
export async function loadPaperTrades(
  client: SupabaseClient,
  accountId: string,
  cursor: string | null,
  size = TRADES_PAGE,
): Promise<Page> {
  if (cursor !== null && !isValidCursor(cursor)) throw new Error("Invalid cursor");
  let query = ledger(client, accountId)
    .in("type", ["pnl", "liquidation"])
    .or("payload->>position_event.is.null,payload->>position_event.eq.closed");
  if (cursor !== null) query = query.lt("seq", Number(cursor));
  const { page, nextCursor } = pageOf(
    await rows<LedgerRow>(
      query.order("seq", { ascending: false }).limit(size + 1),
      "closed trades",
    ),
    size,
  );
  const ids = [
    ...new Set(page.map((line) => line.setup_id).filter((id): id is string => Boolean(id))),
  ];
  const lines: LedgerRow[] = [];
  for (let offset = 0; offset < ids.length; offset += SETUP_CHUNK) {
    lines.push(
      ...(await rows<LedgerRow>(
        ledger(client, accountId)
          .in("setup_id", ids.slice(offset, offset + SETUP_CHUNK))
          .in("type", MONEY_KINDS)
          .order("seq", { ascending: true })
          .limit(1000),
        "trade lines",
      )),
    );
  }
  return { lines, strategies: await strategiesOf(client, ids), nextCursor };
}

/** One page of recorded non-trades (rejected setups, orders and fills), newest first. */
export async function loadPaperRejected(
  client: SupabaseClient,
  accountId: string,
  cursor: string | null,
  size = REJECTED_PAGE,
): Promise<Page> {
  if (cursor !== null && !isValidCursor(cursor)) throw new Error("Invalid cursor");
  let query = ledger(client, accountId).in("type", ["rejected", "order_rejected"]);
  if (cursor !== null) query = query.lt("seq", Number(cursor));
  const { page, nextCursor } = pageOf(
    await rows<LedgerRow>(query.order("seq", { ascending: false }).limit(size + 1), "rejections"),
    size,
  );
  return {
    lines: page,
    strategies: await strategiesOf(
      client,
      page.map((line) => line.setup_id ?? ""),
    ),
    nextCursor,
  };
}

/** One page of raw ledger lines (every kind), newest first. */
export async function loadPaperLedger(
  client: SupabaseClient,
  accountId: string,
  cursor: string | null,
  size = LEDGER_PAGE,
): Promise<Page> {
  if (cursor !== null && !isValidCursor(cursor)) throw new Error("Invalid cursor");
  let query = ledger(client, accountId);
  if (cursor !== null) query = query.lt("seq", Number(cursor));
  const { page, nextCursor } = pageOf(
    await rows<LedgerRow>(query.order("seq", { ascending: false }).limit(size + 1), "ledger"),
    size,
  );
  return { lines: page, strategies: {}, nextCursor };
}
