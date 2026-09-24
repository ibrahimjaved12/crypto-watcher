import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
import { PGlite } from "@electric-sql/pglite";

const db = new PGlite();
const user = "00000000-0000-0000-0000-000000000001";
const other = "00000000-0000-0000-0000-000000000002";
const hash = "a".repeat(64);

before(async () => {
  await db.exec(`
    CREATE ROLE anon;
    CREATE ROLE authenticated;
    CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth;
    CREATE TABLE auth.users (id uuid PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql AS
      'SELECT nullif(current_setting(''request.jwt.claim.sub'', true), '''')::uuid';
    CREATE TABLE public.market_instruments (
      id text PRIMARY KEY,
      native_symbol text NOT NULL,
      UNIQUE (id, native_symbol)
    );
    GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
    GRANT SELECT ON public.market_instruments TO authenticated, service_role;
  `);
  await db.exec(
    await readFile(
      new URL("../supabase/migrations/20260924110000_analysis_conclusions.sql", import.meta.url),
      "utf8",
    ),
  );
  await db.query("INSERT INTO auth.users VALUES ($1), ($2)", [user, other]);
  await db.exec("INSERT INTO market_instruments VALUES ('binance-usdm:BTCUSDT','BTCUSDT')");
});
after(() => db.close());

async function insertConclusion(uid, key, supersedes = null) {
  return (
    await db.query(
      `INSERT INTO analysis_conclusions (
        user_id,idempotency_key,supersedes_id,instrument_id,symbol,provider,
        source_instrument_id,source_native_symbol,endpoint,timeframe_minutes,
        direction,classification,reference_price,price_type,source_event_at,
        completed_candle_at,detected_at,evaluated_at,source_retrieved_at,
        source_freshness_ms,status,score,factor_breakdown,reasons,ta_version,
        strategy_version,schema_version,configuration_version,input_hash
      ) VALUES (
        $1,$2,$3,'binance-usdm:BTCUSDT','BTCUSDT','binance-usdm',
        'binance-usdm:BTCUSDT','BTCUSDT','/fapi/v1/klines',15,
        'bullish','trend_up',100,'trade','2026-09-24T00:15:00Z',
        '2026-09-24T00:15:00Z','2026-09-24T00:15:01Z','2026-09-24T00:15:01Z',
        '2026-09-24T00:15:01Z',1000,'ok',25,'{"trend":{"contribution":25}}',
        '["trend_up"]','ta-v2','interpretation-v1',1,'default-v1',$4
      ) RETURNING *`,
      [uid, key, supersedes, hash],
    )
  ).rows[0];
}

test("conclusions are isolated, append-only, linked and idempotency-keyed", async () => {
  const original = await insertConclusion(user, "btc-15m-1");
  await db.query(
    `INSERT INTO analysis_conclusions (
      user_id,idempotency_key,instrument_id,symbol,provider,source_instrument_id,
      source_native_symbol,endpoint,timeframe_minutes,direction,classification,
      price_type,detected_at,evaluated_at,status,factor_breakdown,reasons,
      ta_version,strategy_version,schema_version,configuration_version,input_hash
    ) VALUES ($1,'btc-15m-1','binance-usdm:BTCUSDT','BTCUSDT','binance-usdm',
      'binance-usdm:BTCUSDT','BTCUSDT','/fapi/v1/klines',15,'bullish','trend_up',
      'trade',now(),now(),'ok','{}','[]','ta-v2','interpretation-v1',1,'default-v1',$2)
    ON CONFLICT (user_id,idempotency_key) DO NOTHING`,
    [user, hash],
  );
  assert.equal(
    Number(
      (await db.query("SELECT count(*) FROM analysis_conclusions WHERE user_id=$1", [user])).rows[0]
        .count,
    ),
    1,
  );

  const revision = await insertConclusion(user, "btc-15m-2", original.id);
  assert.equal(revision.supersedes_id, original.id);
  await assert.rejects(insertConclusion(other, "cross-account", original.id), /foreign key/);
  await insertConclusion(other, "btc-15m-1");

  await db.query("SELECT set_config('request.jwt.claim.sub',$1,false)", [user]);
  await db.exec("SET ROLE authenticated");
  try {
    const visible = (await db.query("SELECT id FROM analysis_conclusions")).rows;
    assert.deepEqual(visible.map((row) => row.id).sort(), [original.id, revision.id].sort());
    await assert.rejects(insertConclusion(user, "browser-write"), /permission denied/);
  } finally {
    await db.exec("RESET ROLE");
  }

  await assert.rejects(
    db.query("UPDATE analysis_conclusions SET score=99 WHERE id=$1", [original.id]),
    /immutable/,
  );
  await assert.rejects(
    db.query("DELETE FROM analysis_conclusions WHERE id=$1", [original.id]),
    /immutable/,
  );
});

async function moduleUrl(path, imports = {}) {
  const source = await readFile(new URL(path, import.meta.url), "utf8");
  let { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  });
  for (const [specifier, target] of Object.entries(imports))
    outputText = outputText.replaceAll(`"${specifier}"`, JSON.stringify(target));
  return `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`;
}

function mockClient(responses) {
  return {
    from() {
      const query = {
        insert() {
          return query;
        },
        select() {
          return query;
        },
        eq() {
          return query;
        },
        maybeSingle() {
          return Promise.resolve(responses.shift());
        },
      };
      return query;
    },
  };
}

test("server append path returns the original row for an exact retry", async () => {
  const emptyAdmin = `data:text/javascript;base64,${Buffer.from(
    "export const supabaseAdmin = {};",
  ).toString("base64")}`;
  const { persistAnalysisConclusion } = await import(
    await moduleUrl("../src/lib/analysis-conclusions.server.ts", {
      "@/integrations/supabase/client.server": emptyAdmin,
    })
  );
  const input = { user_id: user, idempotency_key: "retry", input_hash: hash };
  const saved = { ...input, input_reference: null, id: "saved-id" };
  const result = await persistAnalysisConclusion(
    input,
    mockClient([
      { data: null, error: { code: "23505" } },
      { data: saved, error: null },
    ]),
  );
  assert.equal(result.id, "saved-id");
  await assert.rejects(
    persistAnalysisConclusion(
      { ...input, input_hash: "b".repeat(64) },
      mockClient([
        { data: null, error: { code: "23505" } },
        { data: saved, error: null },
      ]),
    ),
    /reused for different input/,
  );
});
