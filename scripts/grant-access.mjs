#!/usr/bin/env node
// Market access labels (#411): nobody has access by default; each user gets one label, crypto, forex or all.
//   node scripts/grant-access.mjs <email> --label crypto|forex|all [--expires 2027-01-01]
//   node scripts/grant-access.mjs <email> --revoke
//   node scripts/grant-access.mjs --list
// Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (from the environment or .env.production.local). Never prints keys.
import { pathToFileURL } from "node:url";

const LABELS = ["crypto", "forex", "all"];
const USAGE = "Usage: node scripts/grant-access.mjs <email> --label crypto|forex|all [--expires DATE] | <email> --revoke | --list";

export function parseArgs(argv) {
  if (argv.length === 1 && argv[0] === "--list") return { list: true };
  const [email, ...rest] = argv;
  if (!email || email.startsWith("--") || !email.includes("@")) throw new Error(USAGE);
  const options = { email: email.trim().toLowerCase(), revoke: false };
  for (let i = 0; i < rest.length; i += 1) {
    const flag = rest[i];
    if (flag === "--revoke") { options.revoke = true; continue; }
    const value = rest[i + 1];
    if (flag !== "--label" && flag !== "--expires") throw new Error(`Unknown flag ${flag}. ${USAGE}`);
    if (value === undefined || value.startsWith("--")) throw new Error(`${flag} needs a value`);
    i += 1;
    if (flag === "--label" && LABELS.includes(value)) options.label = value;
    else if (flag === "--expires" && !Number.isNaN(Date.parse(value))) options.expires = new Date(value).toISOString();
    else throw new Error(`Invalid ${flag} ${value}`);
  }
  if (options.revoke && (options.label || options.expires)) throw new Error(`--revoke takes no other flags. ${USAGE}`);
  if (!options.revoke && !options.label) throw new Error(USAGE);
  return options;
}

function loadEnv() {
  if (process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY) return;
  try {
    process.loadEnvFile(".env.production.local");
  } catch {
    // missing file: reported below
  }
}

function client() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key)
    throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required (set them, or put them in .env.production.local)");
  const headers = { apikey: key, "Content-Type": "application/json",
    ...(!key.startsWith("sb_secret_") ? { Authorization: `Bearer ${key}` } : {}) };
  return async (path, init = {}, what = path.split("?")[0]) => {
    const response = await fetch(new URL(path, url), { ...init, headers: { ...headers, ...init.headers },
      signal: AbortSignal.timeout(30_000) });
    if (!response.ok) throw new Error(`${init.method ?? "GET"} ${what} failed (HTTP ${response.status})`);
    const text = await response.text();
    return text ? JSON.parse(text) : null;
  };
}

/** Every auth user, page by page (the admin API has no email filter). */
async function* authUsers(call) {
  for (let page = 1; ; page += 1) {
    const body = await call(`/auth/v1/admin/users?page=${page}&per_page=1000`, {}, "auth admin users");
    const users = body?.users ?? [];
    yield* users;
    if (users.length < 1000) return;
  }
}

async function findUser(call, email) {
  for await (const user of authUsers(call)) if (user.email?.toLowerCase() === email) return user.id;
  throw new Error("No auth user with that email");
}

async function list(call) {
  const rows = await call("/rest/v1/user_access?select=user_id,plan,expires_at&order=updated_at.desc");
  if (!rows?.length) return console.log("No user has access.");
  const emails = new Map();
  for await (const user of authUsers(call)) emails.set(user.id, user.email ?? "(no email)");
  for (const row of rows)
    console.log(`${emails.get(row.user_id) ?? "(unknown user)"}  ${row.plan}  ${row.expires_at ?? "no expiry"}`);
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  loadEnv();
  const call = client();
  if (options.list) return list(call);
  const userId = await findUser(call, options.email);
  if (options.revoke) {
    await call(`/rest/v1/user_access?user_id=eq.${userId}`, { method: "DELETE" });
  } else {
    const row = { user_id: userId, plan: options.label, expires_at: options.expires ?? null,
      updated_at: new Date().toISOString() };
    await call("/rest/v1/user_access?on_conflict=user_id", { method: "POST",
      headers: { Prefer: "resolution=merge-duplicates,return=minimal" }, body: JSON.stringify(row) });
  }
  const markets = await call("/rest/v1/rpc/markets_of", { method: "POST", body: JSON.stringify({ p_user: userId }) });
  console.log(`${options.revoke ? "Revoked" : `Set label ${options.label}`} for ${options.email}: markets ${JSON.stringify(markets)}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
