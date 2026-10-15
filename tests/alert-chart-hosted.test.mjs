// Hosted copy of movement-alert charts (#298): view model per state, the upload hook, ownership of the signed URL,
// delete, caps, upload failure handling and the hosted migration (PGlite). Every candle/number here is SYNTHETIC.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { after, before, beforeEach, test } from "node:test";
import { PGlite } from "@electric-sql/pglite";

import { createModuleLoader } from "./module-loader.mjs";

const load = createModuleLoader(import.meta.url);
const hosted = await import(load("../src/lib/alert-charts/hosted.ts"));
const server = await import(load("../src/lib/alert-charts/hosted.server.ts"));
const upload = await import(load("../src/lib/alert-charts/upload.server.ts"));

const NOW = Date.parse("2026-10-16T12:00:00Z");
const ALERT_ID = "0b6f5d55-7a43-4a9e-9d43-7c9f3f0c1a11";
const OTHER_ALERT = "1c7f6e66-8b54-4bab-8e54-8d0f4f1d2b22";
const USER = "11111111-1111-4111-8111-111111111111";
const OTHER = "22222222-2222-4222-8222-222222222222";
const alert = { id: ALERT_ID, symbol: "ETHUSDT", triggered_at: "2026-10-16T11:30:00Z", is_test: false, comparison_mode: "baseline",
  change_pct: 2.1, threshold_pct: 2, price: 102.1, baseline_price: 100, baseline_at: "2026-10-16T09:00:00Z",
  observed_at: "2026-10-16T11:29:00Z", rule: "up >= 2% from saved baseline", data_source: "binance-usdm" };
const meta = (patch = {}) => ({ alert_id: ALERT_ID, status: "pending", rendered_local: false, error: null, bytes: null,
  rendered_at: null, created_at: "2026-10-16T11:30:00Z", updated_at: "2026-10-16T11:30:00Z", ...patch });

// ---------------------------------------------------------------- pure view model

test("every chart state has an explicit view, and only ready charts load an image and show the dot", () => {
  const view = (a, m) => hosted.chartViewModel(a, m, NOW);
  assert.equal(view(alert, undefined).state, "none");
  assert.match(view(alert, undefined).body, /older than the chart feature|capture was off/);
  assert.equal(view({ ...alert, is_test: true }, meta()).state, "none");
  const waiting = view(alert, meta());
  assert.deepEqual([waiting.state, waiting.ready, waiting.loadImage, waiting.changing], ["waiting", false, false, true]);
  assert.match(waiting.body, /30 min/); // the age
  assert.equal(view(alert, meta({ status: "rendering", updated_at: "2026-10-16T11:58:00Z" })).state, "rendering");
  const local = view(alert, meta({ rendered_local: true }));
  assert.equal(local.state, "not_uploaded");
  assert.equal(local.title, "Chart not uploaded yet");
  const failed = view(alert, meta({ status: "failed", error: "WindowError: bad window" }));
  assert.equal(failed.state, "failed");
  assert.match(failed.body, /bad window/);
  assert.equal(view(alert, meta({ status: "capped", error: null })).state, "capped");
  const expired = view(alert, meta({ created_at: "2026-10-01T00:00:00Z" }));
  assert.equal(expired.state, "expired");
  const ready = view(alert, meta({ status: "ready", bytes: 60_000, rendered_at: "2026-10-16T11:31:00Z" }));
  assert.deepEqual([ready.state, ready.ready, ready.loadImage, ready.changing], ["ready", true, true, false]);
  for (const state of ["none", "waiting", "rendering", "not_uploaded", "failed", "capped", "expired"]) {
    const sample = { none: undefined, waiting: meta(), rendering: meta({ status: "rendering" }), not_uploaded: meta({ rendered_local: true }),
      failed: meta({ status: "failed" }), capped: meta({ status: "capped" }), expired: meta({ created_at: "2026-01-01T00:00:00Z" }) }[state];
    const model = view(alert, sample);
    assert.equal(model.state, state);
    assert.ok(model.title.length > 0 && model.body.length > 0); // never a blank box
    assert.equal(model.ready, false);
  }
});

test("alert facts carry UTC time, baseline, price, change, threshold, rule version and source", () => {
  const facts = hosted.alertFacts(alert);
  assert.equal(facts.firedAtUtc, "2026-10-16 11:29 UTC");
  assert.equal(facts.baselineAtUtc, "2026-10-16 09:00 UTC");
  assert.deepEqual([facts.baselinePrice, facts.alertPrice, facts.changePct, facts.thresholdPct], [100, 102.1, 2.1, 2]);
  assert.equal(facts.ruleVersion, "cumulative-baseline-v1");
  assert.equal(facts.dataSource, "binance-usdm");
  assert.equal(hosted.alertFacts({ ...alert, baseline_price: null, baseline_at: null, price: null }).baselineAtUtc, null);
});

test("hosted caps default to 5000 charts and 1 GiB and are bounded", () => {
  assert.deepEqual(hosted.hostedCaps({}), { maxCharts: 5000, maxBytes: 1024 ** 3 });
  assert.equal(hosted.hostedCaps({ ALERT_CHART_HOSTED_MAX_CHARTS: "10" }).maxCharts, 10);
  assert.throws(() => hosted.hostedCaps({ ALERT_CHART_HOSTED_MAX_CHARTS: "0" }));
});

// ---------------------------------------------------------------- the upload hook

function hookDeps({ begin = { ok: true, reason: "ok", storage_path: `${USER}/${ALERT_ID}.png` }, size = 60_000, signed = true } = {}) {
  const calls = [];
  return { calls, deps: {
    rpc: async (name, args) => {
      calls.push([name, args]);
      return { data: name === "begin_alert_chart_upload" ? [begin] : true, error: null };
    },
    signedUploadUrl: async () => (signed ? { signedUrl: "https://x.supabase.co/storage/v1/object/upload/sign/alert-charts/p.png?token=t", token: "t" } : null),
    objectSize: async () => size,
    publishableKey: "sb_publishable_test", caps: { maxCharts: 5, maxBytes: 1_000_000 },
  } };
}

test("the hook accepts only the shared machine secret and validated small inputs", () => {
  const env = { RUNNER_GATEWAY_ENABLED: "true", RUNNER_GATEWAY_SECRET: "s".repeat(40) };
  assert.equal(server.hookAuthorized("s".repeat(40), env), true);
  assert.equal(server.hookAuthorized("s".repeat(39) + "x", env), false);
  assert.equal(server.hookAuthorized(null, env), false);
  assert.equal(server.hookAuthorized("s".repeat(40), { ...env, RUNNER_GATEWAY_ENABLED: "false" }), false);
  assert.equal(server.parseHookInput({ op: "begin", alertId: "not-a-uuid", bytes: 5 }), null);
  assert.equal(server.parseHookInput({ op: "begin", alertId: ALERT_ID, bytes: 10_000_000 }), null);
  assert.equal(server.parseHookInput({ op: "complete", alertId: ALERT_ID, bytes: 5, sha256: "zz" }), null);
  assert.equal(server.parseHookInput({ op: "other", alertId: ALERT_ID }), null);
  assert.deepEqual(server.parseHookInput({ op: "status", alertId: ALERT_ID, status: "failed", error: "x\ny", renderedLocal: false }),
    { op: "status", alertId: ALERT_ID, status: "failed", error: "x y", renderedLocal: false });
});

test("begin returns a signed upload URL for the owner's path; the cap is reported and nothing is stored", async () => {
  const ok = hookDeps();
  const reply = await server.handleChartHook(ok.deps, { op: "begin", alertId: ALERT_ID, bytes: 60_000 });
  assert.equal(reply.status, 200);
  assert.equal(reply.body.path, `${USER}/${ALERT_ID}.png`);
  assert.match(reply.body.uploadUrl, /upload\/sign/);
  assert.equal(reply.body.apikey, "sb_publishable_test");
  assert.deepEqual(ok.calls[0][1], { p_alert_id: ALERT_ID, p_bytes: 60_000, p_max_charts: 5, p_max_bytes: 1_000_000 });
  const capped = hookDeps({ begin: { ok: false, reason: "cap", storage_path: null } });
  const refused = await server.handleChartHook(capped.deps, { op: "begin", alertId: ALERT_ID, bytes: 60_000 });
  assert.deepEqual([refused.status, refused.body.ok, refused.body.category], [409, false, "cap"]);
  assert.match(refused.body.error, /nothing was deleted/);
  const gone = hookDeps({ begin: { ok: false, reason: "alert_gone", storage_path: null } });
  assert.equal((await server.handleChartHook(gone.deps, { op: "begin", alertId: ALERT_ID, bytes: 1 })).status, 404);
});

test("complete publishes only when the stored object has exactly the announced size", async () => {
  const good = hookDeps({ size: 60_000 });
  const input = { op: "complete", alertId: ALERT_ID, bytes: 60_000, sha256: "a".repeat(64) };
  assert.equal((await server.handleChartHook(good.deps, input)).status, 200);
  assert.ok(good.calls.some(([name]) => name === "complete_alert_chart_upload"));
  const wrong = hookDeps({ size: 59_999 });
  const reply = await server.handleChartHook(wrong.deps, input);
  assert.deepEqual([reply.status, reply.body.category], [422, "size_mismatch"]);
  assert.ok(!wrong.calls.some(([name]) => name === "complete_alert_chart_upload"));
  assert.equal((await server.handleChartHook(hookDeps({ size: null }).deps, input)).status, 422);
});

// ---------------------------------------------------------------- signed URL ownership and delete

test("the signed URL is only given to the owner; another user's alert id looks like an unknown one", async () => {
  const row = { user_id: USER, status: "ready", storage_path: `${USER}/${ALERT_ID}.png` };
  const signed = [];
  const deps = {
    chartRow: async (id) => (id === ALERT_ID ? row : null),
    signedUrls: async (path, filename) => { signed.push([path, filename]); return { view: "https://s/view", download: "https://s/dl" }; },
  };
  const own = await server.chartImageFor(deps, USER, ALERT_ID, "ETHUSDT");
  assert.deepEqual([own.ok, own.viewUrl, own.downloadUrl, own.expiresInSeconds], [true, "https://s/view", "https://s/dl", 600]);
  assert.match(signed[0][1], /^ETHUSDT-alert-0b6f5d55\.png$/);
  signed.length = 0;
  await assert.rejects(server.chartImageFor(deps, OTHER, ALERT_ID), /Chart not found/);
  await assert.rejects(server.chartImageFor(deps, USER, OTHER_ALERT), /Chart not found/); // same message as a foreign id
  await assert.rejects(server.chartImageFor(deps, USER, "nope"), /Chart not found/);
  assert.equal(signed.length, 0); // nothing was signed for the refused calls
  const pending = await server.chartImageFor({ ...deps, chartRow: async () => ({ ...row, status: "pending", storage_path: null }) }, USER, ALERT_ID);
  assert.deepEqual(pending, { ok: false, reason: "not_ready" });
  assert.deepEqual(await server.chartImageFor({ ...deps, signedUrls: async () => null }, USER, ALERT_ID), { ok: false, reason: "missing_file" });
});

test("deleting an alert removes its stored chart first; if the file cannot be removed the alert is kept", async () => {
  const events = [];
  const deps = (over = {}) => ({
    chartRow: async () => ({ user_id: USER, storage_path: `${USER}/${ALERT_ID}.png` }),
    removeObject: async (path) => { events.push(`remove:${path}`); },
    deleteAlert: async (id) => { events.push(`delete:${id}`); return 1; },
    ...over,
  });
  assert.deepEqual(await server.deleteAlertAndChart(deps(), USER, ALERT_ID), { deleted: true, chartRemoved: true });
  assert.deepEqual(events, [`remove:${USER}/${ALERT_ID}.png`, `delete:${ALERT_ID}`]);
  events.length = 0;
  const noChart = await server.deleteAlertAndChart(deps({ chartRow: async () => null }), USER, ALERT_ID);
  assert.deepEqual([noChart.deleted, noChart.chartRemoved, events], [true, false, [`delete:${ALERT_ID}`]]);
  events.length = 0;
  // another user's chart row is never removed by this caller
  await server.deleteAlertAndChart(deps({ chartRow: async () => ({ user_id: OTHER, storage_path: "x/y.png" }) }), USER, ALERT_ID);
  assert.deepEqual(events, [`delete:${ALERT_ID}`]);
  events.length = 0;
  await assert.rejects(server.deleteAlertAndChart(deps({ removeObject: async () => { throw new Error("storage down"); } }), USER, ALERT_ID), /storage down/);
  assert.deepEqual(events, []); // the alert was not deleted
});

// ---------------------------------------------------------------- machine-side upload client

const png = new Uint8Array(Array.from({ length: 2048 }, (_, i) => (i * 7) % 256));
const sha = createHash("sha256").update(png).digest("hex");
const job = { chart_id: "c1", event_id: ALERT_ID, png_path: "/charts/a.png", png_sha256: sha, png_bytes: png.length, upload_attempts: 0 };
const config = { origin: "https://site.example", secret: "s".repeat(40) };

function fakeFetch(routes) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    const handler = routes(String(url), init);
    if (handler instanceof Error) throw handler;
    return new Response(JSON.stringify(handler.body ?? {}), { status: handler.status });
  };
  return { fetch, calls };
}

test("upload: begin, signed PUT without a hosted key, complete; the local file is only read", async () => {
  let read = 0;
  const net = fakeFetch((url, init) => {
    if (url.endsWith("/api/public/hooks/alert-chart-upload")) {
      const body = JSON.parse(init.body);
      if (body.op === "begin") return { status: 200, body: { ok: true, uploadUrl: "https://x.supabase.co/storage/v1/object/upload/sign/alert-charts/p.png?token=t", apikey: "sb_publishable_k" } };
      return { status: 200, body: { ok: true } };
    }
    return { status: 200 };
  });
  const result = await upload.uploadChart({ hosted: config, fetch: net.fetch, readFile: async () => { read += 1; return png; } }, job);
  assert.deepEqual(result, { ok: true });
  assert.equal(read, 1);
  assert.deepEqual(net.calls.map((c) => c.init.method), ["POST", "PUT", "POST"]);
  assert.equal(net.calls[0].init.headers["x-runner-secret"], config.secret);
  assert.equal(net.calls[1].init.headers.apikey, "sb_publishable_k");
  assert.ok(!JSON.stringify(net.calls[1].init.headers).includes(config.secret)); // the secret never goes to storage
  assert.equal(JSON.parse(net.calls[2].init.body).sha256, sha);
});

test("upload failures keep the local chart: offline and 5xx are transient, cap/deleted alert/changed file are permanent", async () => {
  const deps = (routes) => ({ hosted: config, fetch: fakeFetch(routes).fetch, readFile: async () => png });
  const offline = await upload.uploadChart(deps(() => new Error("fetch failed")), job);
  assert.deepEqual([offline.ok, offline.permanent], [false, false]);
  const down = await upload.uploadChart(deps(() => ({ status: 503, body: {} })), job);
  assert.deepEqual([down.ok, down.permanent], [false, false]);
  const storage = await upload.uploadChart(deps((url) => (url.includes("alert-chart-upload")
    ? { status: 200, body: { ok: true, uploadUrl: "https://x.supabase.co/u?token=t", apikey: "k" } } : { status: 500 })), job);
  assert.deepEqual([storage.ok, storage.permanent, storage.error], [false, false, "storage answered 500"]);
  const capped = await upload.uploadChart(deps(() => ({ status: 409, body: { ok: false, category: "cap", error: "The per-account chart limit is reached" } })), job);
  assert.deepEqual([capped.ok, capped.permanent], [false, true]);
  const gone = await upload.uploadChart(deps(() => ({ status: 404, body: { ok: false, error: "The upload was refused." } })), job);
  assert.equal(gone.permanent, true);
  const changed = await upload.uploadChart({ hosted: config, fetch: fakeFetch(() => ({ status: 200 })).fetch, readFile: async () => new Uint8Array([1, 2, 3]) }, job);
  assert.deepEqual([changed.ok, changed.permanent], [false, true]);
  const missing = await upload.uploadChart({ hosted: config, fetch: fakeFetch(() => ({ status: 200 })).fetch, readFile: async () => { throw new Error("ENOENT"); } }, job);
  assert.deepEqual([missing.ok, missing.permanent], [false, true]);
  // a status report never throws when the site is unreachable
  await upload.reportChartStatus({ hosted: config, fetch: async () => { throw new Error("offline"); }, readFile: async () => png }, ALERT_ID, "rendering");
});

// ---------------------------------------------------------------- hosted migration (PGlite)

const db = new PGlite();
const countRows = async (sql, args = []) => (await db.query(sql, args)).rows[0].n;
before(async () => {
  await db.exec(`CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
    CREATE SCHEMA auth; CREATE TABLE auth.users(id UUID PRIMARY KEY);
    CREATE FUNCTION auth.uid() RETURNS UUID LANGUAGE SQL AS $$ SELECT NULLIF(current_setting('test.uid', true), '')::uuid $$;
    CREATE SCHEMA storage; CREATE TABLE storage.buckets(id TEXT PRIMARY KEY, name TEXT, public BOOLEAN,
      file_size_limit BIGINT, allowed_mime_types TEXT[]);
    GRANT USAGE ON SCHEMA public, auth TO anon, authenticated, service_role;
    CREATE TABLE public.alerts(id UUID PRIMARY KEY DEFAULT gen_random_uuid(), user_id UUID NOT NULL REFERENCES auth.users(id),
      is_test BOOLEAN NOT NULL DEFAULT false);
    GRANT ALL ON public.alerts TO service_role; GRANT SELECT ON public.alerts TO authenticated;
    INSERT INTO auth.users VALUES ('${USER}'), ('${OTHER}');`);
  await db.exec(await readFile(new URL("../supabase/migrations/20261016150000_alert_chart_files.sql", import.meta.url), "utf8"));
});
beforeEach(() => db.exec("RESET ROLE; TRUNCATE public.alerts CASCADE"));
after(() => db.close());

const addAlert = async (user = USER, isTest = false) =>
  (await db.query("INSERT INTO public.alerts(user_id, is_test) VALUES ($1,$2) RETURNING id", [user, isTest])).rows[0].id;
const begin = async (id, bytes = 50_000, charts = 5, maxBytes = 1_000_000) =>
  (await db.query("SELECT * FROM begin_alert_chart_upload($1,$2,$3,$4)", [id, bytes, charts, maxBytes])).rows[0];
const file = async (id) => (await db.query("SELECT * FROM alert_chart_files WHERE alert_id = $1", [id])).rows[0];

test("hosted migration: private bucket, owner-only metadata, service-role-only writes", async () => {
  const bucket = (await db.query("SELECT * FROM storage.buckets WHERE id = 'alert-charts'")).rows[0];
  assert.deepEqual([bucket.public, Number(bucket.file_size_limit), bucket.allowed_mime_types], [false, 524288, ["image/png"]]);
  const mine = await addAlert(USER);
  const theirs = await addAlert(OTHER);
  await db.exec("SET ROLE service_role");
  await db.query("SELECT register_alert_chart_pending($1)", [mine]);
  await db.query("SELECT register_alert_chart_pending($1)", [theirs]);
  await db.exec("RESET ROLE; SET ROLE authenticated");
  await db.exec(`SET test.uid = '${USER}'`);
  assert.deepEqual((await db.query("SELECT alert_id FROM alert_chart_files")).rows.map((r) => r.alert_id), [mine]); // RLS
  await assert.rejects(db.query("UPDATE alert_chart_files SET status = 'ready'"));
  await assert.rejects(db.query("DELETE FROM alert_chart_files"));
  await assert.rejects(db.query("SELECT register_alert_chart_pending($1)", [mine]));
  await assert.rejects(db.query("SELECT * FROM begin_alert_chart_upload($1,1,1,1)", [mine]));
  await db.exec("RESET ROLE; SET ROLE anon");
  await assert.rejects(db.query("SELECT * FROM alert_chart_files"));
});

test("register is idempotent, skips test alerts, and begin refuses unknown or deleted alerts", async () => {
  await db.exec("SET ROLE service_role");
  const id = await addAlertAsOwner();
  assert.equal((await db.query("SELECT register_alert_chart_pending($1) AS ok", [id])).rows[0].ok, true);
  assert.equal((await db.query("SELECT register_alert_chart_pending($1) AS ok", [id])).rows[0].ok, false);
  const testAlert = await addAlertAsOwner(USER, true);
  assert.equal((await db.query("SELECT register_alert_chart_pending($1) AS ok", [testAlert])).rows[0].ok, false);
  assert.equal(await file(testAlert), undefined);
  assert.deepEqual((await begin("00000000-0000-4000-8000-000000000000")).reason, "alert_gone");
  assert.equal((await begin(id, 600_000)).reason, "bad_size");
});
async function addAlertAsOwner(user = USER, isTest = false) {
  await db.exec("RESET ROLE");
  const id = await addAlert(user, isTest);
  await db.exec("SET ROLE service_role");
  return id;
}

test("begin points at the owner's path, complete publishes it, and a status report never downgrades a ready chart", async () => {
  await db.exec("SET ROLE service_role");
  const id = await addAlertAsOwner();
  const started = await begin(id);
  assert.deepEqual([started.ok, started.storage_path], [true, `${USER}/${id}.png`]);
  assert.equal((await file(id)).status, "pending");
  await db.query("SELECT report_alert_chart_status($1,'rendering',NULL,false)", [id]);
  assert.equal((await file(id)).status, "rendering");
  await db.query("SELECT report_alert_chart_status($1,'failed','WindowError: bad',false)", [id]);
  assert.deepEqual([(await file(id)).status, (await file(id)).error], ["failed", "WindowError: bad"]);
  await db.query("SELECT complete_alert_chart_upload($1,$2,50000)", [id, "a".repeat(64)]);
  const ready = await file(id);
  assert.deepEqual([ready.status, ready.storage_path, ready.bytes, ready.error, ready.rendered_local], ["ready", `${USER}/${id}.png`, 50000, null, false]);
  await db.query("SELECT report_alert_chart_status($1,'rendering',NULL,false)", [id]);
  assert.equal((await file(id)).status, "ready");
  await assert.rejects(db.query("SELECT report_alert_chart_status($1,'ready',NULL,false)", [id]));
});

test("the per-account cap refuses new uploads visibly and deletes nothing; another account is unaffected", async () => {
  await db.exec("SET ROLE service_role");
  const first = await addAlertAsOwner();
  const second = await addAlertAsOwner();
  const third = await addAlertAsOwner(OTHER);
  for (const id of [first]) {
    await begin(id, 50_000, 1, 1_000_000);
    await db.query("SELECT complete_alert_chart_upload($1,$2,50000)", [id, "b".repeat(64)]);
  }
  const refused = await begin(second, 50_000, 1, 1_000_000); // chart cap 1 reached
  assert.deepEqual([refused.ok, refused.reason], [false, "cap"]);
  const capped = await file(second);
  assert.deepEqual([capped.status, capped.error], ["capped", "Your stored chart limit is reached"]);
  assert.equal((await file(first)).status, "ready"); // nothing deleted
  assert.equal((await begin(third, 50_000, 1, 1_000_000)).ok, true); // per account
  const bytesCap = await begin(second, 50_000, 10, 60_000); // 50k stored + 50k > 60k bytes
  assert.equal(bytesCap.reason, "cap");
  assert.equal((await begin(first, 50_000, 1, 60_000)).ok, true); // replacing its own chart does not count twice
});

test("deleting the alert deletes its chart metadata", async () => {
  await db.exec("SET ROLE service_role");
  const id = await addAlertAsOwner();
  await begin(id);
  await db.exec("RESET ROLE");
  await db.query("DELETE FROM public.alerts WHERE id = $1", [id]);
  assert.equal(await countRows("SELECT count(*)::int AS n FROM alert_chart_files"), 0);
});
