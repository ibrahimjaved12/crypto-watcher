type Env = Record<string, string | undefined>;

/** Consume one slot for a key; rejected attempts do not extend the window. */
export function createRateLimiter(
  { windowMs, max }: { windowMs: number; max: number },
  now: () => number,
) {
  const windows = new Map<string, number[]>();
  return (key: string): { ok: boolean; retryAfterSeconds: number } => {
    const time = now();
    // Expire idle keys as well as the current key, without a background timer.
    for (const [id, entries] of windows) {
      const live = entries.filter((at) => at > time - windowMs);
      if (live.length) windows.set(id, live);
      else windows.delete(id);
    }
    const entries = windows.get(key) ?? [];
    if (entries.length >= max) {
      return { ok: false, retryAfterSeconds: Math.max(1, Math.ceil((entries[0]! + windowMs - time) / 1000)) };
    }
    entries.push(time);
    windows.set(key, entries);
    return { ok: true, retryAfterSeconds: 0 };
  };
}

export function secretMatches(provided: string, expected: string): boolean {
  if (provided.length !== expected.length) return false;
  let difference = 0;
  for (let i = 0; i < expected.length; i++) difference |= provided.charCodeAt(i) ^ expected.charCodeAt(i);
  return difference === 0;
}

/** Problems contain variable names and static descriptions, never configuration values. */
export function validateGatewayEnv(env: Env): string[] {
  const problems: string[] = [];
  if (!/^[A-Za-z0-9_-]{32,256}$/.test(env.RUNNER_GATEWAY_SECRET ?? ""))
    problems.push("RUNNER_GATEWAY_SECRET must be 32–256 URL-safe characters.");
  const port = env.RUNNER_GATEWAY_PORT ?? "8787";
  if (!/^\d+$/.test(port) || !Number.isInteger(Number(port)) || Number(port) < 1024 || Number(port) > 65535)
    problems.push("RUNNER_GATEWAY_PORT must be an integer from 1024 to 65535.");
  for (const name of ["SUPABASE_URL", "SUPABASE_PUBLISHABLE_KEY", "SUPABASE_SERVICE_ROLE_KEY"])
    if (!env[name]?.trim()) problems.push(`${name} is required.`);
  return problems;
}
