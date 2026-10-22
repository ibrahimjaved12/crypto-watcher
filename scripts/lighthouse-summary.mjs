// Sticky PR comment for the Lighthouse workflow (#463): reads every scores-*/scores.json written by
// lighthouse-run.mjs and prints one Mobile and one Desktop table (crypto rows, then forex, in PAGES order).
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";

function getArgument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const scoresDir = getArgument("--scores-dir") ?? "lh-scores";
const hostUp = getArgument("--host-up") ?? "";
const baseUrl = (getArgument("--base-url") ?? "").replace(/\/+$/, "");
const runUrl = getArgument("--run-url") ?? "";
const pages = (getArgument("--pages") ?? "")
  .split(/\r?\n/)
  .map((entry) => entry.trim())
  .filter(Boolean)
  .map((entry) => {
    const [slug = "", label = "", pagePath = ""] = entry.split("|").map((part) => part.trim());
    return { slug, label, path: pagePath };
  })
  .filter((page) => page.slug && page.label && page.path);

const MARKETS = [
  ["crypto", "🪙 Crypto"],
  ["forex", "💱 Forex"],
];
const MARKET_NAME = { crypto: "Crypto", forex: "Forex" };
const DEVICES = [
  ["mobile", "📱 Mobile"],
  ["desktop", "🖥️ Desktop"],
];
const CATEGORIES = ["performance", "accessibility", "bestPractices"];

async function findScoreFiles(directory) {
  let entries;
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch {
    return [];
  }
  const found = [];
  for (const entry of entries) {
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) found.push(...(await findScoreFiles(full)));
    else if (entry.name === "scores.json") found.push(full);
  }
  return found;
}

const runs = new Map();
for (const file of await findScoreFiles(scoresDir)) {
  try {
    const data = JSON.parse(await readFile(file, "utf8"));
    if (data && data.market && data.device) runs.set(`${data.market}-${data.device}`, data);
  } catch {
    // an unreadable file counts as missing
  }
}

function scoreCell(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "⚠️ failed";
  const rounded = Math.max(0, Math.min(100, Math.round(value)));
  const band = rounded >= 90 ? "🟢" : rounded >= 50 ? "🟠" : "🔴";
  return `${band} ${rounded}`;
}

const lines = [
  "<!-- lighthouse-check -->",
  "### 🔦 Lighthouse",
  "",
  "*Scores are for the deployed production site (main).*",
  "",
];

if (hostUp === "false") {
  lines.push("🔴 Host server is down, Lighthouse did not run.");
  process.stdout.write(`${lines.join("\n")}\n`);
  process.exit(0);
}

const notes = [];
const statuses = new Set([...runs.values()].map((run) => run.status));
if (statuses.has("no-credentials")) notes.push("⚠️ Test sign-in is not set up.");
if (statuses.has("login-failed")) notes.push("⚠️ Test sign-in failed.");
for (const [market] of MARKETS) {
  const missing = [...runs.values()].some(
    (run) => run.status === "market-unavailable" && run.market === market,
  );
  if (missing) notes.push(`⚠️ Test account has no access to ${MARKET_NAME[market]}.`);
}
if (notes.length) lines.push(...notes, "");

for (const [device, heading] of DEVICES) {
  lines.push(
    `#### ${heading}`,
    "",
    "| Page | Performance | Accessibility | Best Practices |",
    "| --- | ---: | ---: | ---: |",
  );
  for (const [market, marketLabel] of MARKETS) {
    const run = runs.get(`${market}-${device}`);
    for (const page of pages) {
      const result = run?.status === "ok" ? run.pages?.[page.slug] : undefined;
      const cells = CATEGORIES.map((category) =>
        result && !result.error ? scoreCell(result[category]) : "⚠️ failed",
      );
      lines.push(
        `| ${marketLabel} · [${page.label}](${baseUrl}${page.path}) | ${cells.join(" | ")} |`,
      );
    }
  }
  lines.push("");
}

lines.push(`[Full reports](${runUrl}) · Scores can vary a few points between runs`);
process.stdout.write(`${lines.join("\n")}\n`);

if (runs.size === 0 && hostUp === "true") process.exitCode = 1;
