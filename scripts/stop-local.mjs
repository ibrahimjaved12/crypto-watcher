import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
for (const args of [["stop"], ["stop", "--workdir", "operational-db"]]) {
  const result = spawnSync("supabase", args, { cwd: root, stdio: "inherit" });
  if (result.error || result.status !== 0) process.exitCode = 1;
}
