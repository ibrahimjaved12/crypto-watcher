/** Parse an environment flag without letting missing/invalid values override its safe default. */
export function activityEnabled(value: string | undefined, enabledWhenUnset = true): boolean {
  const normalized = value?.trim().toLowerCase();
  if (normalized === "true") return true;
  if (normalized === "false") return false;
  return enabledWhenUnset;
}

/** enabled:false also blocks mount, focus, reconnect and invalidation fetches. */
export function automaticQueryOptions(value: string | undefined) {
  const enabled = activityEnabled(value, false);
  return {
    enabled,
    refetchInterval: enabled ? 60_000 : (false as const),
    ...(enabled ? {} : { retry: false as const }),
  };
}

/** Only fixed operation names and decisions; never request/user data. */
export function logActivity(
  diagnostics: string | undefined,
  operation: "ta-history" | "market" | "scheduled-monitor" | "ta-generation" | "ta-outcomes",
  action: "automatic-paused" | "request-started" | "skipped",
) {
  if (diagnostics === "true") console.info("[activity-controls]", { operation, action });
}
