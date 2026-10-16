import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";

// #36 Part 3: the Paper account loaders, against a small in-memory stand-in for the Supabase client.
const source = await readFile(new URL("../src/lib/paper/paper-data.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 } });
const m = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`);

const OR_CLOSED = "payload->>position_event.is.null,payload->>position_event.eq.closed";
const compare = (x, y) => (x < y ? -1 : x > y ? 1 : 0);

function client({ tables = {}, rpc = {}, errors = {} } = {}) {
  const calls = { from: [], rpc: [], selects: [] };
  return {
    calls,
    from(table) {
      calls.from.push(table);
      const filters = [];
      const orders = [];
      let max = Infinity;
      const query = {
        select(columns) { calls.selects.push([table, columns]); return query; },
        eq(key, value) { filters.push((row) => row[key] === value); return query; },
        in(key, values) { filters.push((row) => values.includes(row[key])); return query; },
        lt(key, value) { filters.push((row) => row[key] < value); return query; },
        or(filter) {
          assert.equal(filter, OR_CLOSED, "the only or-filter is the closing-line rule");
          filters.push((row) => row.payload?.position_event === undefined || row.payload.position_event === "closed");
          return query;
        },
        order(key, { ascending }) { orders.push([key, ascending]); return query; },
        limit(n) { max = n; return query; },
        then(resolve) {
          if (errors[table]) return Promise.resolve({ data: null, error: { message: errors[table] } }).then(resolve);
          const rows = (tables[table] ?? []).filter((row) => filters.every((keep) => keep(row))).sort((x, y) => {
            for (const [key, ascending] of orders) {
              const result = compare(x[key], y[key]);
              if (result) return ascending ? result : -result;
            }
            return 0;
          });
          return Promise.resolve({ data: rows.slice(0, max), error: null }).then(resolve);
        },
      };
      return query;
    },
    rpc(name, args) {
      calls.rpc.push([name, args]);
      const answer = rpc[name];
      return Promise.resolve(answer instanceof Error ? { data: null, error: { message: answer.message } } : { data: answer, error: null });
    },
  };
}

const summaryRow = {
  trades: "2", target: "1", stopped: "1", expired: "0", liquidated: "0", ambiguous: "0", unresolved: "0", wins: "1", losses: "1",
  sum_net_e8: "-205", gross_wins_e8: "80", gross_losses_e8: "-285", rejected: "3", order_rejected: "1",
  open_fees_e8: "-50", close_fees_e8: "-30", funding_e8: "-5", liquidation_e8: "0", pnl_e8: "75", lines: "9",
  first_ms: "1000", last_ms: "5000",
};

test("the account read takes the latest OK run, converts SQL numbers and the price rows, and buckets by span", async () => {
  const tables = {
    forward_market_setups: [{ setup_id: "p1", strategy_id: "macd_12_26_9:60", horizon_min: 60 }, { setup_id: "zz", strategy_id: "x", horizon_min: 15 }],
    paper_runs: [
      { status: "ok", boundary_ms: 100, created_at: "a", paper_account: "acct", wallet_config: { leverage_cap: 20 },
        wallet_state: { balance_e8: 1, positions: { p1: { symbol: "BTCUSDT" } } } },
      { status: "skipped_stale", boundary_ms: 200, created_at: "b", paper_account: "acct", wallet_state: {} },
    ],
    market_quotes: [{ symbol: "BTCUSDT", price: 100.5, published_at: "2026-10-16T10:00:00Z" }, { symbol: "LTCUSDT", price: 1, published_at: "x" }],
  };
  const hourly = client({ tables, rpc: { paper_account_summary: [summaryRow],
    paper_equity_series: [{ ms: "3600000", balance_e8: "10000000000", seq: 1, bucket_ms: 0 }] } });
  const data = await m.loadPaperAccount(hourly);
  assert.equal(data.accountId, "acct");
  assert.equal(data.run.boundary_ms, 100);
  assert.equal(data.run.wallet_config.leverage_cap, 20);
  assert.deepEqual([data.summary.trades, data.summary.sum_net_e8, data.summary.first_ms, data.summary.lines], [2, -205, 1000, 9]);
  assert.deepEqual(data.equity, [{ ms: 3_600_000, balance_e8: 10_000_000_000 }]);
  assert.deepEqual(data.positionMeta, { p1: { strategy_id: "macd_12_26_9:60", horizon_min: 60 } }, "only the open positions' setups");
  assert.deepEqual(data.quotes, [{ symbol: "BTCUSDT", price: 100.5, published_at: "2026-10-16T10:00:00Z" }]);
  assert.deepEqual(hourly.calls.rpc[1], ["paper_equity_series", { p_bucket_ms: 3_600_000, p_from_ms: null }]);

  const daily = client({ tables, rpc: { paper_account_summary: [{ ...summaryRow, first_ms: "0", last_ms: String(61 * 86_400_000) }],
    paper_equity_series: [] } });
  await m.loadPaperAccount(daily);
  assert.equal(daily.calls.rpc[1][1].p_bucket_ms, 86_400_000, "more than 60 days: daily points");
});

test("a failing totals call or price read degrades that part only; a failing wallet read is an error", async () => {
  const tables = { paper_runs: [{ status: "ok", boundary_ms: 100, created_at: "a", paper_account: "acct", wallet_state: {} }] };
  const partial = await m.loadPaperAccount(client({ tables, errors: { market_quotes: "prices down" },
    rpc: { paper_account_summary: new Error("function does not exist") } }));
  assert.equal(partial.run.boundary_ms, 100);
  assert.equal(partial.summary, null);
  assert.match(partial.summaryError, /does not exist/);
  assert.deepEqual([partial.quotes, partial.equity], [[], []]);
  await assert.rejects(m.loadPaperAccount(client({ errors: { paper_runs: "boom" } })), /wallet read failed: boom/);
  const none = await m.loadPaperAccount(client({ rpc: { paper_account_summary: [{ ...summaryRow, lines: "0", first_ms: null, last_ms: null }] } }));
  assert.deepEqual([none.accountId, none.run, none.equity], [null, null, []]);
});

const ledger = [
  { seq: 1, setup_id: "s1", type: "open_fee", ms: 1000, paper_account: "acct", payload: {} },
  { seq: 2, setup_id: "s2", type: "open_fee", ms: 1100, paper_account: "acct", payload: {} },
  { seq: 3, setup_id: "s2", type: "funding", ms: 1500, paper_account: "acct", payload: {} },
  { seq: 4, setup_id: "s3", type: "open_fee", ms: 1200, paper_account: "acct", payload: {} },
  { seq: 5, setup_id: "s1", type: "pnl", ms: 2000, paper_account: "acct", payload: { outcome: "T" } },
  { seq: 6, setup_id: "s1", type: "close_fee", ms: 2000, paper_account: "acct", payload: {} },
  { seq: 7, setup_id: "s4", type: "open_fee", ms: 1300, paper_account: "acct", payload: {} },
  { seq: 8, setup_id: "s4", type: "pnl", ms: 2300, paper_account: "acct", payload: { position_event: "reduced" } },
  { seq: 10, setup_id: "s2", type: "pnl", ms: 2100, paper_account: "acct", payload: { outcome: "S" } },
  { seq: 11, setup_id: "s2", type: "close_fee", ms: 2100, paper_account: "acct", payload: {} },
  { seq: 12, setup_id: "r1", type: "rejected", ms: 2400, paper_account: "acct", symbol: "BTCUSDT", payload: { reason: "cap:max_positions" } },
  { seq: 13, setup_id: "r2", type: "order_rejected", ms: 2500, paper_account: "acct", payload: { reason: "limit_not_passive" } },
  { seq: 20, setup_id: "s3", type: "liquidation", ms: 1800, paper_account: "acct", payload: {} },
  { seq: 99, setup_id: "s1", type: "pnl", ms: 9999, paper_account: "other", payload: { outcome: "T" } },
];
const setups = [{ setup_id: "s2", strategy_id: "macd_12_26_9:60" }, { setup_id: "s3", strategy_id: "rsi_14_reversion:15" },
  { setup_id: "r1", strategy_id: "ema_cross_20_50:15" }];

test("closed trades page by closing line and arrive with every money line of their setups", async () => {
  const stub = client({ tables: { paper_ledger: ledger, forward_market_setups: setups } });
  const first = await m.loadPaperTrades(stub, "acct", null, 2);
  assert.equal(first.nextCursor, "10");
  assert.deepEqual(first.lines.map((line) => line.seq), [2, 3, 4, 10, 11, 20], "complete lines of s2 and s3, oldest first");
  assert.deepEqual(first.strategies, { s2: "macd_12_26_9:60", s3: "rsi_14_reversion:15" });
  const second = await m.loadPaperTrades(stub, "acct", first.nextCursor, 2);
  assert.equal(second.nextCursor, null);
  assert.deepEqual(second.lines.map((line) => line.seq), [1, 5, 6], "s1 only: the partial reduction of s4 and the other account are not trades");
  assert.ok(stub.calls.selects.some(([table, columns]) => table === "paper_ledger" && columns.includes("payload")));
  await assert.rejects(m.loadPaperTrades(stub, "acct", "1; drop table", 2), /Invalid cursor/);
});

test("rejections and the ledger page newest first, scoped to the account", async () => {
  const stub = client({ tables: { paper_ledger: ledger, forward_market_setups: setups } });
  const rejected = await m.loadPaperRejected(stub, "acct", null, 1);
  assert.deepEqual([rejected.lines.map((line) => line.seq), rejected.nextCursor], [[13], "13"]);
  const rest = await m.loadPaperRejected(stub, "acct", "13", 5);
  assert.deepEqual([rest.lines.map((line) => line.seq), rest.nextCursor, rest.strategies], [[12], null, { r1: "ema_cross_20_50:15" }]);
  const lines = await m.loadPaperLedger(stub, "acct", null, 4);
  assert.deepEqual(lines.lines.map((line) => line.seq), [20, 13, 12, 11]);
  assert.equal(lines.nextCursor, "11");
  const all = await m.loadPaperLedger(stub, "acct", "11", 50);
  assert.equal(all.nextCursor, null);
  assert.ok(all.lines.every((line) => line.paper_account === "acct" && line.seq < 11));
  assert.deepEqual(m.toSummary({}).trades, 0);
  assert.equal(m.isValidCursor("12"), true);
  assert.equal(m.isValidCursor("-1"), false);
});
