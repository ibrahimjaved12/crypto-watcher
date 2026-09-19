/** Unset preserves existing behavior; only an explicit false pauses work. */
export function activityEnabled(value: string | undefined): boolean {
  return value?.trim().toLowerCase() !== "false";
}

/** enabled:false also blocks mount, focus, reconnect and invalidation fetches. */
export function automaticQueryOptions(value: string | undefined) {
  const enabled = activityEnabled(value);
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
