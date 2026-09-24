import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "../node_modules/typescript/lib/typescript.js";
const source = readFileSync(new URL("../src/lib/environment.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
});
const { validateEnvironment, validateTarget } = await import(
  `data:text/javascript;base64,${Buffer.from(outputText).toString("base64")}`
);
const local = {
  APP_PROFILE: "local",
  VITE_APP_PROFILE: "local",
  SUPABASE_URL: "http://127.0.0.1:54321",
  VITE_SUPABASE_URL: "http://127.0.0.1:54321",
  SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test",
  VITE_SUPABASE_PUBLISHABLE_KEY: "sb_publishable_test",
};
test("fresh configuration fails; local loopback succeeds", () => {
  assert.throws(() => validateEnvironment({}), /APP_PROFILE/);
  validateEnvironment(local);
  for (const host of ["localhost", "[::1]", "127.0.0.1"])
    validateTarget("local", "false", `http://${host}:54321`, "sb_publishable_test");
});
test("local rejects hosted targets even with opt-in and lookalike hosts", () => {
  for (const host of ["project.supabase.co", "localhost.example.com", "127.0.0.1.example.com"]) {
    assert.throws(
      () => validateTarget("local", "true", `https://${host}`, "sb_publishable_test"),
      /loopback/,
    );
  }
});
test("hosted profile requires opt-in and HTTPS", () => {
  assert.throws(
    () => validateTarget("production", undefined, "https://project.supabase.co", "key"),
    /ALLOW_HOSTED/,
  );
  assert.throws(
    () => validateTarget("production", "true", "http://project.supabase.co", "key"),
    /HTTPS/,
  );
  validateTarget("production", "true", "https://project.supabase.co", "sb_publishable_test");
});
test("browser and runtime mismatches fail before client creation", () => {
  for (const changes of [
    { SUPABASE_URL: "http://localhost:54321" },
    { SUPABASE_PUBLISHABLE_KEY: "different" },
    { APP_PROFILE: "production" },
  ]) {
    assert.throws(() => validateEnvironment({ ...local, ...changes }), /differ/);
  }
});
test("rejects server secrets by public name, copied value, opaque key and JWT role", () => {
  for (const name of [
    "VITE_SUPABASE_SERVICE_ROLE_KEY",
    "VITE_MONITOR_CRON_TOKEN",
    "VITE_PYTHON_ANALYSIS_TOKEN",
    "VITE_LOVABLE_CRON_SECRET",
  ]) {
    assert.throws(() => validateEnvironment({ ...local, [name]: "private" }), /server secret/);
  }
  assert.throws(
    () =>
      validateEnvironment({ ...local, PYTHON_ANALYSIS_TOKEN: "private", VITE_ACCIDENT: "private" }),
    /server secret/,
  );
  for (const key of [
    "sb_secret_private",
    `e30.${Buffer.from(JSON.stringify({ role: "service_role" })).toString("base64url")}.signature`,
  ]) {
    assert.throws(
      () =>
        validateEnvironment({
          ...local,
          SUPABASE_PUBLISHABLE_KEY: key,
          VITE_SUPABASE_PUBLISHABLE_KEY: key,
        }),
      /server secret|secret key|JWT/,
    );
  }
  validateEnvironment({ ...local, SUPABASE_SERVICE_ROLE_KEY: "private" });
});

test("operational database configuration has no browser-visible variants", () => {
  for (const name of [
    "VITE_OPERATIONAL_DB_ENABLED",
    "VITE_OPERATIONAL_SUPABASE_URL",
    "VITE_OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
  ]) {
    assert.throws(() => validateEnvironment({ ...local, [name]: "configured" }), /server-only/);
  }
});
test("URLs reject credentials and non-origin targets", () => {
  for (const url of [
    "ftp://localhost",
    "http://user:pass@localhost",
    "http://localhost/path",
    "http://localhost?key=secret",
  ])
    assert.throws(() => validateTarget("local", "false", url, "key"), /origin/);
});

test("generator uses local status, protects permissions, and refuses overwrite/hosted status", async () => {
  const { mkdtempSync, mkdirSync, writeFileSync, statSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { spawnSync } = await import("node:child_process");
  const root = mkdtempSync(join(tmpdir(), "crypto-env-test-"));
  try {
    const bin = join(root, "bin");
    mkdirSync(bin);
    const cli = join(bin, "supabase");
    const run = () =>
      spawnSync(
        process.execPath,
        [fileURLToPath(new URL("../scripts/local-env.mjs", import.meta.url))],
        {
          cwd: root,
          env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
          encoding: "utf8",
        },
      );
    const runOperational = () =>
      spawnSync(
        process.execPath,
        [fileURLToPath(new URL("../scripts/operational-env.mjs", import.meta.url))],
        {
          cwd: root,
          env: { ...process.env, PATH: `${bin}:${process.env.PATH}` },
          encoding: "utf8",
        },
      );
    const mock = (url, operationalUrl = "http://127.0.0.1:55321") =>
      writeFileSync(
        cli,
        `#!/bin/sh\ncase " $* " in *" --workdir operational-db "*) printf '%s' '${JSON.stringify({ API_URL: operationalUrl, ANON_KEY: "operational-anon-test", SERVICE_ROLE_KEY: "operational-secret-test" })}' ;; *) printf '%s' '${JSON.stringify({ API_URL: url, ANON_KEY: "anon-test", SERVICE_ROLE_KEY: "secret-test" })}' ;; esac\n`,
        { mode: 0o700 },
      );
    mock("http://127.0.0.1:54321");
    assert.equal(run().status, 0);
    const file = join(root, ".env.local");
    const contents = readFileSync(file, "utf8");
    assert.match(contents, /APP_PROFILE="local"/);
    assert.match(contents, /SUPABASE_SERVICE_ROLE_KEY="secret-test"/);
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.equal(runOperational().status, 0);
    const withOperational = readFileSync(file, "utf8");
    assert.match(withOperational, /OPERATIONAL_SUPABASE_URL="http:\/\/127\.0\.0\.1:55321"/);
    assert.match(
      withOperational,
      /OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY="operational-secret-test"/,
    );
    assert.equal(runOperational().status, 1);
    assert.equal(readFileSync(file, "utf8"), withOperational);
    assert.equal(run().status, 1);
    assert.equal(readFileSync(file, "utf8"), withOperational);
    rmSync(file);
    mock("https://project.supabase.co");
    assert.equal(run().status, 1);
    assert.throws(() => statSync(file), /ENOENT/);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
