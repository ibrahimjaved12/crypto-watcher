/** Hosted heartbeat transport. Only fixed service names and boolean health enter the database. */
export function createRunnerHealthReporter(env) {
  const key = env.SUPABASE_SERVICE_ROLE_KEY;
  const url = new URL("/rest/v1/rpc/report_runner_health", env.SUPABASE_URL);
  const headers = { "Content-Type": "application/json", apikey: key };
  // Opaque Supabase keys use apikey only; legacy service keys are bearer JWTs.
  if (!key.startsWith("sb_secret_")) headers.Authorization = `Bearer ${key}`;
  return async (service, status, services = {}) => {
    const response = await fetch(url, {
      method: "POST",
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(5_000),
      body: JSON.stringify({ p_service: service, p_status: status, p_services: services }),
    });
    await response.body?.cancel();
    if (!response.ok) throw new Error("Runner health could not be published.");
  };
}
