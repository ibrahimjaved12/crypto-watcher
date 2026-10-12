import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createModuleLoader } from "./module-loader.mjs";

const moduleUrl = createModuleLoader(import.meta.url);

const contractUrl = await moduleUrl("../src/lib/forward/forward-contract.ts", { zod: import.meta.resolve("zod") });
const runUrl = await moduleUrl("../src/lib/forward/forward-run.server.ts", { "./forward-contract": contractUrl });
const trafficUrl = await moduleUrl("../src/lib/forward/forward-hosted-traffic.server.ts");
const sharedUrl = await moduleUrl("../src/lib/forward/forward-shared-repository.server.ts", {
  "./forward-contract": contractUrl, "./forward-run.server": runUrl, "./forward-hosted-traffic.server": trafficUrl });
const { createSharedRepository } = await import(sharedUrl);
const { createForwardRepository } = await import(await moduleUrl("../src/lib/forward/forward-repository.server.ts", {
  "./forward-contract": contractUrl, "./forward-run.server": runUrl,
  "./forward-hosted-traffic.server": trafficUrl, "./forward-shared-repository.server": sharedUrl }));
const { createSigmaCache } = await import(await moduleUrl("../src/lib/forward/forward-sigma-cache.server.ts", {
  "./forward-contract": contractUrl }));
const { sigmaCanonical } = await import(contractUrl);
const { jsonBytes } = await import(trafficUrl);
const HOUR = 3_600_000;
function sigma(asOf) {
  const body = { symbol: "BTCUSDT", sigma_version: "forward-sigma-v1", as_of_ms: asOf,
    payload: { source: "store_rebuild", symbol: "BTCUSDT", as_of_ms: String(asOf) } };
  return { ...body, checksum: createHash("sha256").update(sigmaCanonical(body)).digest("hex") };
}
async function withCache(work) {
  const dir = await mkdtemp(join(tmpdir(), "forward-sigma-"));
  try { await work(createSigmaCache(dir), dir); }
  finally { await rm(dir, { recursive: true, force: true }); }
}

test("cache directory uses the supplied logger once across cache instances", async () => {
  const dir = await mkdtemp(join(tmpdir(), "forward-log-")), lines = [];
  try {
    createSigmaCache(dir, () => Date.now(), line => lines.push(line));
    createSigmaCache(dir, () => Date.now(), line => lines.push(line));
    assert.deepEqual(lines, [`[forward-sigma] cache dir=${dir}`]);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test("valid local sigma avoids hosted reads; missing or corrupt files restore and validate", async () => {
  await withCache(async (cache, dir) => {
    let reads = 0;
    const state = sigma(HOUR);
    const restore = async () => { reads++; return { state, checkpointed_at: HOUR }; };
    assert.equal((await cache.load("BTCUSDT", restore)).source, "restored");
    assert.equal((await cache.load("BTCUSDT", restore)).source, "local");
    assert.equal(reads, 1);
    const entry = JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8"));
    assert.deepEqual(entry, { state, hosted: { checksum: state.checksum, as_of_ms: HOUR, checkpointed_at: HOUR } });
    await writeFile(join(dir, "BTCUSDT.json"), JSON.stringify({ ...entry, state: { ...state, checksum: "0".repeat(64) } }));
    assert.equal((await cache.load("BTCUSDT", restore)).source, "restored");
    await writeFile(join(dir, "BTCUSDT.json"), "broken JSON");
    assert.equal((await cache.load("BTCUSDT", restore)).source, "restored");
    assert.equal(reads, 3);
    assert.deepEqual(await readdir(dir), ["BTCUSDT.json"], "no temporary file or lock survives a write");
  });
});

test("only missing hosted state permits rebuild; invalid hosted state is never overwritten", async () => {
  await withCache(async cache => {
    assert.deepEqual(await cache.load("BTCUSDT", async () => null), { state: null, hosted: null, source: "rebuilt" });
    await assert.rejects(cache.load("BTCUSDT", async () => ({ state: { ...sigma(HOUR), checksum: "0".repeat(64) },
      checkpointed_at: HOUR })), /checksum mismatch/);
  });
});

test("atomic writers never decrease as_of_ms, including separate cache instances", async () => {
  await withCache(async (cache, dir) => {
    const other = createSigmaCache(dir);
    await Promise.all([cache.save({ state: sigma(2 * HOUR), hosted: null }),
      other.save({ state: sigma(HOUR), hosted: null })]);
    assert.equal((await cache.load("BTCUSDT", async () => { throw new Error("unexpected restore"); })).state.as_of_ms, 2 * HOUR);
    assert.equal(await cache.save({ state: sigma(HOUR), hosted: null }), false);
    assert.deepEqual(await readdir(dir), ["BTCUSDT.json"]);
  });
});

test("conflicts survive restart, restore hosted next run and retain a monotonic disk cursor", async () => {
  await withCache(async (cache, dir) => {
    const local = sigma(2 * HOUR), remote = sigma(HOUR);
    await cache.save({ state: local, hosted: null });
    await cache.conflict("BTCUSDT");
    assert.deepEqual(JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8")).hosted,
      { checksum: null, as_of_ms: null, checkpointed_at: null });
    const next = createSigmaCache(dir);
    const loaded = await next.load("BTCUSDT", async () => ({ state: remote, checkpointed_at: HOUR }));
    assert.equal(loaded.source, "restored");
    assert.deepEqual(loaded.state, remote, "evaluation rebases on the verified hosted state");
    let file = JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8"));
    assert.equal(file.state.as_of_ms, 2 * HOUR);
    assert.equal(file.hosted.checkpointed_at, null, "a failed stage must still restore next run");
    const advanced = sigma(3 * HOUR);
    await next.save({ state: advanced, hosted: loaded.hosted });
    await next.checkpointed(advanced, 3 * HOUR);
    file = JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8"));
    assert.deepEqual(file, { state: advanced, hosted: { checksum: advanced.checksum, as_of_ms: 3 * HOUR, checkpointed_at: 3 * HOUR } });
  });
});

test("concurrent writers recover a dead process lock without losing monotonic state", async () => {
  await withCache(async (cache, dir) => {
    const lock = join(dir, "BTCUSDT.json.lock");
    await mkdir(lock);
    await writeFile(join(lock, "pid"), "2147483647");
    await Promise.all([cache.save({ state: sigma(2 * HOUR), hosted: null }),
      createSigmaCache(dir).save({ state: sigma(HOUR), hosted: null })]);
    assert.equal(JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8")).state.as_of_ms, 2 * HOUR);
    assert.deepEqual(await readdir(dir), ["BTCUSDT.json"]);
  });
});

function stage(boundary = HOUR) {
  return { id: `stage-${boundary}`, boundary_ms: boundary, freshness: {}, events: [], adverse_paths: [],
    response: { schema_version: 1, versions: { forward: "forward-v1" }, params_hash: null,
      assumptions: [], processed_to_ms: { BTCUSDT: boundary - 60_000 }, funding_unavailable: [],
      signals: [], setups: [], resolutions: [], reasons: {} } };
}
function clientFixture(initial = []) {
  const queries = [], writes = [], rows = initial.map(value => ({ id: value.id, boundary_ms: value.boundary_ms,
    payload: value.response, freshness: value.freshness }));
  const wallet = { version: "wallet-v2", positions: {}, done: ["é".repeat(100)], seq: 1 };
  const latest = { id: "user-run", boundary_ms: 0, consumed_boundary_ms: 0, wallet_state: wallet };
  const account = { user_id: "user", account_id: "user", config: {}, start_boundary_ms: 0 };
  const client = {
    from(table) {
      const query = { table, selection: null, filters: [], count: 500 };
      queries.push(query);
      function result() {
        let data;
        if (table === "forward_wallet_accounts") data = account;
        else if (table === "paper_runs") data = latest;
        else if (table === "forward_sigma_state") data = { ...sigma(HOUR), updated_at: "2026-10-11T00:00:00Z" };
        else if (table === "forward_market_stages") {
          let found = rows.filter(row => query.filters.every(([op, key, value]) =>
            op === "not" ? !value.includes(row[key]) : op === "eq" ? row[key] === value : op === "gt" ? row[key] > value : row[key] <= value));
          if (query.descending) found = found.toSorted((a, b) => b.boundary_ms - a.boundary_ms);
          found = found.slice(0, query.count);
          data = found.map(row => query.selection === "*" ? row : query.selection.includes("payload->")
            ? { id: row.id, boundary_ms: row.boundary_ms, processed_to_ms: row.payload.processed_to_ms }
            : query.selection === "id" ? { id: row.id } : { id: row.id, boundary_ms: row.boundary_ms });
          if (query.single) data = data[0] ?? null;
        } else data = [];
        return { data, error: null };
      }
      const builder = {
        select(value) { query.selection = value; return builder; },
        eq(key, value) { query.filters.push(["eq", key, value]); return builder; },
        gt(key, value) { query.filters.push(["gt", key, value]); return builder; },
        not(key, _operator, value) { query.filters.push(["not", key, value.slice(1, -1).split(",").map(Number)]); return builder; },
        lte(key, value) { query.filters.push(["lte", key, value]); return builder; },
        order(_key, options) { query.descending = options.ascending === false; return builder; },
        limit(value) { query.count = value; return builder; },
        single() { query.single = true; return builder; },
        maybeSingle() { query.single = true; return builder; },
        then(ok, fail) { return Promise.resolve(result()).then(ok, fail); },
      };
      return builder;
    },
    async rpc(name, args) {
      writes.push({ name, args: structuredClone(args) });
      if (name === "commit_market_stage") { rows.push(structuredClone(args.p_stage)); return { data: args.p_stage.id, error: null }; }
      return { data: null, error: null };
    },
  };
  return { client, queries, writes, account, latest, rows };
}

test("market RPC carries empty sigma, measures actual JSON bodies and reuses in-memory stages", async () => {
  const f = clientFixture(), lines = [];
  const repo = createSharedRepository(f.client, undefined, line => lines.push(line));
  const current = stage();
  assert.equal(await repo.stageId(HOUR), null);
  assert.equal(await repo.commitMarket(current, new Map(), new Map()), current.id);
  const args = f.writes[0].args;
  assert.deepEqual(args.p_states, []); assert.deepEqual(args.p_expected, {});
  assert.equal(current.freshness.stage_bytes, jsonBytes(args.p_stage));
  assert.equal(current.freshness.records_bytes, jsonBytes(args.p_records));
  assert.equal(current.freshness.write_bytes, jsonBytes(args));
  assert.equal(await repo.stage(HOUR), current);
  assert.deepEqual(await repo.latestStage(), { id: current.id, boundary_ms: HOUR, processed_to_ms: { BTCUSDT: HOUR - 60_000 } });
  assert.deepEqual(await repo.stagesAfter(0, HOUR, [current]), [current]);
  assert.ok(f.queries.every(q => q.selection !== "*"));
  assert.equal(f.queries.filter(q => /events|paths/.test(q.table)).length, 0);
  assert.match(lines[0], /^\[forward-commit\] boundary=.* stage_bytes=\d+ records_bytes=\d+ events=0 adverse_paths=0 rpc_ms=\d+$/);
});

test("uncached catch-up stages hydrate once across accounts and repository recreation", async () => {
  const f = clientFixture([stage()]);
  const repo = createSharedRepository(f.client, undefined, () => {});
  const first = await repo.stagesAfter(0, HOUR);
  assert.equal((await repo.stagesAfter(0, HOUR))[0], first[0]);
  const next = createSharedRepository(f.client, undefined, () => {});
  assert.equal((await next.stagesAfter(0, HOUR))[0], first[0]);
  assert.equal(f.queries.filter(q => q.selection === "*").length, 1);
  assert.equal(f.queries.filter(q => q.table === "forward_market_events").length, 1);
  assert.equal(f.queries.filter(q => q.table === "forward_market_paths").length, 1);
});

test("cursor JSON must be a map of safe millisecond values, not a malformed replay range", async () => {
  const f = clientFixture([stage()]);
  const repo = createSharedRepository(f.client, undefined, () => {});
  for (const value of ["bad cursor", [], { BTCUSDT: "123" }, { BTCUSDT: -1 }, { BTCUSDT: 2 ** 53 }]) {
    f.rows[0].payload.processed_to_ms = value;
    await assert.rejects(repo.latestStage(), /Invalid market processed cursor/);
  }
  f.rows[0].payload.processed_to_ms = { BTCUSDT: 0 };
  assert.deepEqual((await repo.latestStage()).processed_to_ms, { BTCUSDT: 0 });
});

test("wallet_state reads and daily checkpoint writes are included in paper run freshness", async () => {
  const f = clientFixture();
  const repo = createForwardRepository(f.client, () => {});
  const account = await repo.shared.account("user", HOUR);
  assert.equal(repo.shared.traffic.snapshot().read_bytes, jsonBytes(f.account) + jsonBytes(f.latest));
  const restored = await repo.restoreSigmaState("BTCUSDT");
  assert.deepEqual(restored.state, sigma(HOUR));
  await repo.checkpointSigmaState(sigma(2 * HOUR), restored.state.checksum);
  const current = stage();
  await repo.shared.commitMarket(current, new Map(), new Map());
  await repo.shared.commitUser(account, current, { ...current.response, wallet_state: {}, wallet_config: {}, ledger: [] }, "hourly");
  const request = f.writes.at(-1).args;
  const metrics = repo.shared.traffic.snapshot();
  assert.equal(request.p_run.freshness.read_bytes, metrics.read_bytes);
  assert.equal(request.p_run.freshness.write_bytes, metrics.write_bytes);
  assert.equal(request.p_run.freshness.stage_bytes, current.freshness.stage_bytes);
  assert.equal(request.p_run.freshness.records_bytes, current.freshness.records_bytes);
  assert.equal(metrics.write_bytes, f.writes.reduce((total, value) => total + jsonBytes(value.args), 0));
});

test("invalid higher local cursor is absent before restore and later save comparisons", async () => {
  await withCache(async (cache, dir) => {
    const remote = sigma(2 * HOUR);
    await writeFile(join(dir, "BTCUSDT.json"), JSON.stringify({ state: { ...sigma(5 * HOUR), checksum: "0".repeat(64) }, hosted: null }));
    const result = await cache.load("BTCUSDT", async () => ({ state: remote, checkpointed_at: 2 * HOUR }));
    assert.equal(result.source, "restored");
    assert.equal(result.state.as_of_ms, 2 * HOUR);
    assert.deepEqual(JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8")).state, remote);
    assert.equal(await cache.save({ state: sigma(3 * HOUR), hosted: result.hosted }), true);
  });
});

test("valid future state and malformed hosted metadata restore rather than count as local", async () => {
  await withCache(async (_cache, dir) => {
    const cache = createSigmaCache(dir, () => 4 * HOUR), remote = sigma(2 * HOUR);
    for (const entry of [
      { state: sigma(5 * HOUR), hosted: null },
      { state: sigma(3 * HOUR), hosted: { checksum: null, as_of_ms: null, checkpointed_at: HOUR } },
      { state: sigma(3 * HOUR), hosted: { checksum: remote.checksum, as_of_ms: null, checkpointed_at: null } },
    ]) {
      await writeFile(join(dir, "BTCUSDT.json"), JSON.stringify(entry));
      const result = await cache.load("BTCUSDT", async () => ({ state: remote, checkpointed_at: 2 * HOUR }));
      assert.equal(result.source, "restored");
      assert.deepEqual(JSON.parse(await readFile(join(dir, "BTCUSDT.json"), "utf8")).state, remote);
    }
  });
});

test("default cache finds the nearest repository ancestor and FORWARD_STATE_DIR overrides lookup", async () => {
  const dir = await mkdtemp(join(tmpdir(), "forward-root-"));
  const cwd = process.cwd(), env = process.env.FORWARD_STATE_DIR;
  try {
    delete process.env.FORWARD_STATE_DIR;
    const root = join(dir, "repo"), nested = join(root, "src", "nested");
    await mkdir(nested, { recursive: true });
    await mkdir(join(root, "supabase"));
    await writeFile(join(root, "package.json"), "{}");
    process.chdir(nested);
    await createSigmaCache().save({ state: sigma(HOUR), hosted: null });
    assert.equal(JSON.parse(await readFile(join(root, ".forward-state", "BTCUSDT.json"), "utf8")).state.as_of_ms, HOUR);
    process.chdir(dir);
    assert.throws(() => createSigmaCache(), /FORWARD_STATE_DIR/);
    process.env.FORWARD_STATE_DIR = join(dir, "override");
    await createSigmaCache().save({ state: sigma(2 * HOUR), hosted: null });
    assert.equal(JSON.parse(await readFile(join(dir, "override", "BTCUSDT.json"), "utf8")).state.as_of_ms, 2 * HOUR);
  } finally {
    process.chdir(cwd);
    if (env === undefined) delete process.env.FORWARD_STATE_DIR; else process.env.FORWARD_STATE_DIR = env;
    await rm(dir, { recursive: true, force: true });
  }
});

test("lagging accounts fetch only stages absent from the supplied catch-up array", async () => {
  const values = [stage(HOUR), stage(2 * HOUR), stage(3 * HOUR)];
  const f = clientFixture(values), repo = createSharedRepository(f.client, undefined, () => {});
  const result = await repo.stagesAfter(0, 3 * HOUR, values.slice(1));
  assert.deepEqual(result.map(value => value.id), values.map(value => value.id));
  assert.equal(result[1], values[1]); assert.equal(result[2], values[2]);
  assert.equal(f.queries.filter(query => query.selection === "*").length, 1);
  assert.deepEqual(f.queries[0].filters.find(filter => filter[0] === "not")[2], [2 * HOUR, 3 * HOUR]);
});

test("cache read still throws non-ENOENT I/O errors", async () => {
  await withCache(async (cache, dir) => {
    await mkdir(join(dir, "BTCUSDT.json"));
    await assert.rejects(cache.load("BTCUSDT", async () => null), { code: "EISDIR" });
  });
});
