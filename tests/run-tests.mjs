import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));
const files = readdirSync(directory)
  .filter((file) => file.endsWith(".test.mjs"))
  .sort();
let failures = 0;

for (const file of files) {
  console.log(`\nRunning ${file}`);
  const result = spawnSync(process.execPath, [join(directory, file)], {
    cwd: directory,
    stdio: "inherit",
  });
  if (result.error || result.status !== 0) {
    failures += 1;
    if (result.error) console.error(result.error);
  }
}

console.log(`\n${files.length - failures}/${files.length} Node test files passed`);
if (failures) process.exitCode = 1;
