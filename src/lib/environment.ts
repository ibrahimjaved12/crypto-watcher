type Env = Record<string, string | boolean | undefined>;

function fail(message: string): never {
  throw new Error(`[Environment] ${message}`);
}

function isPrivilegedKey(value: unknown): boolean {
  if (typeof value !== "string") return false;
  if (value.startsWith("sb_secret_")) return true;
  try {
    const payload = value.split(".")[1];
    return (
      !!payload &&
      JSON.parse(atob(payload.replace(/-/g, "+").replace(/_/g, "/"))).role === "service_role"
    );
  } catch {
    return false;
  }
}

export function validatePublicSecrets(env: Env) {
  for (const name of [
    "VITE_OPERATIONAL_DB_ENABLED",
    "VITE_OPERATIONAL_SUPABASE_URL",
    "VITE_OPERATIONAL_SUPABASE_SERVICE_ROLE_KEY",
    "VITE_BINANCE_COLLECTOR_ENABLED",
    "VITE_MOVEMENT_FINALIZATION_GRACE_MS",
  ]) {
    if (env[name] !== undefined) fail(`${name} must remain server-only.`);
  }
  const secrets = Object.entries(env)
    .filter(
      ([name, value]) =>
        !name.startsWith("VITE_") &&
        /SECRET|TOKEN|SERVICE_ROLE|PASSWORD|PRIVATE_KEY/.test(name) &&
        typeof value === "string" &&
        value.length > 0,
    )
    .map(([, value]) => value);
  for (const [name, value] of Object.entries(env)) {
    if (!name.startsWith("VITE_")) continue;
    if (
      /SECRET|TOKEN|SERVICE_ROLE|PASSWORD|PRIVATE_KEY/.test(name) ||
      (value && secrets.includes(value)) ||
      isPrivilegedKey(value)
    ) {
      fail(`${name} exposes a server secret. Remove it from browser configuration.`);
    }
  }
}

export function validateTarget(profile: unknown, optIn: unknown, rawUrl: unknown, key: unknown) {
  if (profile !== "local" && profile !== "production")
    fail("APP_PROFILE must be local or production. Run npm run env:local for local setup.");
  let url: URL;
  try {
    url = new URL(String(rawUrl));
  } catch {
    return fail("Supabase URL is missing or invalid. Run npm run env:local.");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  )
    fail("Supabase URL must be an HTTP(S) origin.");
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  if (profile === "local" && !loopback)
    fail(
      "Local profile requires a loopback Supabase URL; hosted access requires the explicit production profile.",
    );
  if (!loopback && (optIn !== "true" || url.protocol !== "https:"))
    fail("Hosted Supabase requires ALLOW_HOSTED_SUPABASE=true and HTTPS.");
  if (typeof key !== "string" || !key)
    fail("Supabase publishable key is missing. Run npm run env:local.");
  if (key.startsWith("sb_secret_")) fail("A secret key cannot be used as a publishable key.");
  if (key.split(".").length === 3) {
    try {
      const payload = JSON.parse(atob(key.split(".")[1]!.replace(/-/g, "+").replace(/_/g, "/")));
      if (payload.role !== "anon") fail("The public Supabase JWT must have the anon role.");
    } catch {
      fail("Invalid public Supabase JWT (only anon keys are permitted).");
    }
  }
  return url.origin;
}

export function validateEnvironment(env: Env, browser: Env = env) {
  validatePublicSecrets(env);
  const serverUrl = validateTarget(
    env["APP_PROFILE"],
    env["ALLOW_HOSTED_SUPABASE"],
    env["SUPABASE_URL"],
    env["SUPABASE_PUBLISHABLE_KEY"],
  );
  const browserUrl = validateTarget(
    browser["VITE_APP_PROFILE"],
    browser["VITE_ALLOW_HOSTED_SUPABASE"],
    browser["VITE_SUPABASE_URL"],
    browser["VITE_SUPABASE_PUBLISHABLE_KEY"],
  );
  if (
    env["APP_PROFILE"] !== browser["VITE_APP_PROFILE"] ||
    serverUrl !== browserUrl ||
    env["SUPABASE_PUBLISHABLE_KEY"] !== browser["VITE_SUPABASE_PUBLISHABLE_KEY"]
  ) {
    fail(
      "Browser and server Supabase targets/profiles/keys differ. Configure both for the same environment and rebuild.",
    );
  }
}
