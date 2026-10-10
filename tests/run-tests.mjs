import { spawn } from "node:child_process";
import { readdirSync } from "node:fs";
import { availableParallelism } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Runs every *.test.mjs in its own process, a bounded number at a time (CPU count, or the first
// argument / TEST_JOBS). Output is buffered per file and printed when the file completes; the
// exit code and the final "N/M Node test files passed" line are unchanged. Slowest files start
// first (seconds measured on CI; unknown files start last) so the pool never ends on a long tail.
const directory = dirname(fileURLToPath(import.meta.url));
const weight = {
  "collector-worker": 5.7, "operational-store": 5.3, ta: 3.1, "analysis-conclusions": 2.4, "forward-trend-db": 2.1,
  cumulative: 2.0, "monitor-lease": 1.9, "collector-boundary": 1.7, "forward-trend": 1.3,
};
const files = readdirSync(directory)
  .filter((file) => file.endsWith(".test.mjs"))
  .sort();
const queue = [...files].sort((a, b) => (weight[b.replace(".test.mjs", "")] ?? 0.8) - (weight[a.replace(".test.mjs", "")] ?? 0.8));
const jobs = Math.max(1, Number(process.argv[2] ?? process.env["TEST_JOBS"]) || availableParallelism());
let failures = 0;

function runFile(file) {
  return new Promise((resolve) => {
    const started = Date.now();
    const chunks = [];
    const child = spawn(process.execPath, [join(directory, file)], { cwd: directory });
    child.stdout.on("data", (chunk) => chunks.push(chunk));
    child.stderr.on("data", (chunk) => chunks.push(chunk));
    const finish = (code, error) => {
      process.stdout.write(`\nRunning ${file} (${((Date.now() - started) / 1000).toFixed(1)}s)\n${Buffer.concat(chunks)}`);
      if (error) console.error(error);
      if (error || code !== 0) failures += 1;
      resolve();
    };
    child.on("error", (error) => finish(1, error));
    child.on("close", (code) => finish(code));
  });
}

await Promise.all(
  Array.from({ length: Math.min(jobs, queue.length) }, async () => {
    for (let file = queue.shift(); file; file = queue.shift()) await runFile(file);
  }),
);

console.log(`\n${files.length - failures}/${files.length} Node test files passed`);
if (failures) process.exitCode = 1;
