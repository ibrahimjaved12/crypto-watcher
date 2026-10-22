import { readFile } from "node:fs/promises";
import path from "node:path";

function getArgument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const reportDir = getArgument("--dir") ?? "lighthouse-reports";
const baseUrl = getArgument("--base-url") ?? "";
const runUrl = getArgument("--run-url") ?? "";
const pages = (getArgument("--pages") ?? "")
  .split(/\r?\n/)
  .map((entry) => entry.trim())
  .filter(Boolean)
  .map((entry) => {
    const [slug, label, pagePath] = entry.split("|");
    return { slug, label, path: pagePath };
  })
  .filter((page) => page.slug && page.label && page.path !== undefined);
const devices = ["mobile", "desktop"];
const categories = [
  ["performance", "Perf"],
  ["accessibility", "A11y"],
  ["best-practices", "Best"],
  ["seo", "SEO"],
];
const reports = new Map();

for (const page of pages) {
  for (const device of devices) {
    const reportPath = path.join(reportDir, `${page.slug}-${device}.json`);
    try {
      const report = JSON.parse(await readFile(reportPath, "utf8"));
      reports.set(`${page.slug}-${device}`, report);
    } catch {
      reports.set(`${page.slug}-${device}`, null);
    }
  }
}

function scoreCell(report, category) {
  const score = report?.categories?.[category]?.score;
  if (typeof score !== "number" || !Number.isFinite(score)) return "⚠️ failed";

  const roundedScore = Math.max(0, Math.min(100, Math.round(score * 100)));
  const band = roundedScore >= 90 ? "🟢" : roundedScore >= 50 ? "🟠" : "🔴";
  return `${band} ${roundedScore}`;
}

const lines = [
  "<!-- lighthouse-check -->",
  "### 🔦 Lighthouse",
  "",
  "*Scores are for the deployed production site (main), not this PR's code. PR previews are tracked in #290.*",
];

for (const [device, heading] of [
  ["mobile", "📱 Mobile"],
  ["desktop", "🖥️ Desktop"],
]) {
  lines.push(
    "",
    `#### ${heading}`,
    "",
    "| Page | Perf | A11y | Best | SEO |",
    "| --- | ---: | ---: | ---: | ---: |",
  );

  for (const page of pages) {
    const report = reports.get(`${page.slug}-${device}`);
    const link = `[${page.label}](${baseUrl}${page.path})`;
    const scores = report
      ? categories.map(([category]) => scoreCell(report, category))
      : categories.map(() => "⚠️ failed");
    lines.push(`| ${link} | ${scores.join(" | ")} |`);
  }
}

lines.push("", `[Full reports](${runUrl}) · Scores can vary a few points between runs`);
process.stdout.write(`${lines.join("\n")}\n`);

if (![...reports.values()].some(Boolean)) process.exitCode = 1;