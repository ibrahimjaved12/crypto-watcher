#!/usr/bin/env node
// Grant or revoke market access (#374): upserts public.user_access for one auth user, then prints markets_of.
// node scripts/grant-access.mjs <email> [--role owner|admin|member] [--plan free|crypto|forex|pro]
//   [--market forex] [--expires 2027-01-01] [--revoke]
// Needs SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (same as scripts/seed-sigma-state.mjs). Never prints keys.
import { pathToFileURL } from "node:url";

const ROLES = ["owner", "admin", "member"];
const PLANS = ["free", "crypto", "forex", "pro"];
const MARKETS = ["crypto", "forex"];

export function parseArgs(argv) {
  const [email, ...rest] = argv;
  if (!email || email.startsWith("--") || !email.includes("@"))
    throw new Error("Usage: node scripts/grant-access.mjs <email> [--role R] [--plan P] [--market M] [--expires DATE] [--revoke]");
  const options = { email: email.trim().toLowerCase(), markets: [], revoke: false };
  for (let i = 0; i < rest.length; i += 1) {
    const flag = rest[i];
    if (flag === "--revoke") { options.revoke = true; continue; }
    const value = rest[i + 1];
    if (value === undefined || value.startsWith("--")) throw new Error(`${flag} needs a value`);
    i += 1;
    if (flag === "--role" && ROLES.includes(value)) options.role = value;
    else if (flag === "--plan" && PLANS.includes(value)) options.plan = value;
    else if (flag === "--market" && MARKETS.includes(value)) options.markets.push(value);
    else if (flag === "--expires" && !Number.isNaN(Date.parse(value))) options.expires = new Date(value).toISOString();
    else throw new Error(`Invalid ${flag} ${value}`);
  }
  return options;
}

function client() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  if (!url || !key) throw new Error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required");
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

async function findUser(call, email) {
  for (let page = 1; ; page += 1) {
    const body = await call(`/auth/v1/admin/users?page=${page}&per_page=1000`, {}, "auth admin users");
    const users = body?.users ?? [];
    const match = users.find((user) => user.email?.toLowerCase() === email);
    if (match) return match.id;
    if (users.length < 1000) throw new Error("No auth user with that email");
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const call = client();
  const userId = await findUser(call, options.email);
  if (options.revoke) {
    await call(`/rest/v1/user_access?user_id=eq.${userId}`, { method: "DELETE" });
  } else {
    const [current] = await call(`/rest/v1/user_access?user_id=eq.${userId}&select=extra_markets`);
    const extra = [...new Set([...(current?.extra_markets ?? []), ...options.markets])].sort();
    const row = { user_id: userId, extra_markets: extra, updated_at: new Date().toISOString(),
      ...(options.role ? { role: options.role } : {}), ...(options.plan ? { plan: options.plan } : {}),
      ...(options.expires ? { expires_at: options.expires } : {}) };
    await call("/rest/v1/user_access?on_conflict=user_id", { method: "POST",
      headers: { Prefer: "resolution=merge-duplicates,return=minimal" }, body: JSON.stringify(row) });
  }
  const markets = await call("/rest/v1/rpc/markets_of", { method: "POST", body: JSON.stringify({ p_user: userId }) });
  console.log(`${options.revoke ? "Revoked" : "Updated"} access for ${userId}: markets ${JSON.stringify(markets)}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
