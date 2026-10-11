/** Explicit, server-owned capability. Missing or malformed values fail closed. */
export function machineOnlyEnabled(): boolean {
  return process.env["MACHINE_ONLY_ENABLED"] === "true";
}

export function requireMachineOnly(): void {
  if (!machineOnlyEnabled()) {
    throw Response.json({ error: "machine_only" }, { status: 403 });
  }
}
