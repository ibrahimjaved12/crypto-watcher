// Sticky PR comment for the Lighthouse workflow (#463): reads the new scores.json shape written by
// lighthouse-run.mjs and prints one compact HTML table per device, with market group rows and grouped
// performance metric columns. With --timing it prints only the collapsed run-time block for the step summary.
import { readFile } from "node:fs/promises";

function getArgument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

const scoresFile = getArgument("--scores-file") ?? "";
const hostUp = getArgument("--host-up") ?? "";
const baseUrl = (getArgument("--base-url") ?? "").replace(/\/+$/, "");
const runUrl = getArgument("--run-url") ?? "";
const timing = process.argv.includes("--timing");
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
const METRICS = [
  ["largestContentfulPaint", "LCP"],
  ["totalBlockingTime", "TBT"],
  ["cumulativeLayoutShift", "CLS"],
  ["firstContentfulPaint", "FCP"],
  ["speedIndex", "SI"],
];

let scores = null;
try {
  const data = JSON.parse(await readFile(scoresFile, "utf8"));
  if (data && typeof data === "object") scores = data;
} catch {
  // a missing or unreadable file counts as no scores
}
const runs = scores?.runs && typeof scores.runs === "object" ? scores.runs : {};

if (timing) {
  if (scores) {
    const lines = [
      "<details><summary>Run time</summary>",
      "",
      "| Market | Device | Seconds |",
      "| --- | --- | ---: |",
    ];
    for (const [market] of MARKETS) {
      for (const [device] of DEVICES) {
        const run = runs[`${market}-${device}`];
        const total = Object.values(run?.pages ?? {}).reduce(
          (sum, result) => sum + (typeof result?.seconds === "number" ? result.seconds : 0),
          0,
        );
        lines.push(`| ${MARKET_NAME[market]} | ${device} | ${run ? total.toFixed(1) : "-"} |`);
      }
    }
    if (typeof scores.totalSeconds === "number") lines.push(`| **Total** | | ${scores.totalSeconds.toFixed(1)} |`);
    lines.push("", "</details>");
    process.stdout.write(`${lines.join("\n")}\n`);
  }
  process.exit(0);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"]/g, (character) => {
    const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" };
    return entities[character];
  });
}

function scoreCell(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "⚠️ failed";
  const rounded = Math.max(0, Math.min(100, Math.round(value)));
  const band = rounded >= 90 ? "🟢" : rounded >= 50 ? "🟠" : "🔴";
  return `${band}&nbsp;${escapeHtml(rounded)}`;
}

function metricCell(metric) {
  if (typeof metric?.text !== "string" || !metric.text) return "—";
  const text = escapeHtml(metric.text.replace(/\s/g, ""));
  if (!text) return "—";
  const band =
    typeof metric.score !== "number"
      ? ""
      : metric.score >= 0.9
        ? "🟢"
        : metric.score >= 0.5
          ? "🟠"
          : "🔴";
  return band ? `${band}&nbsp;${text}` : text;
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
const status = scores?.status;
if (status === "no-credentials") notes.push("⚠️ Test sign-in is not set up.");
if (status === "login-failed") notes.push("⚠️ Test sign-in failed.");
if (status === "no-access") notes.push("⚠️ Test account has no market access.");
for (const [market] of MARKETS) {
  const missing = DEVICES.some(([device]) => runs[`${market}-${device}`]?.status === "market-unavailable");
  if (missing) notes.push(`⚠️ Test account has no access to ${MARKET_NAME[market]}.`);
}
if (scores?.runner === "offline") notes.push("🔴 Runner is offline.");
if (notes.length) lines.push(...notes, "");

for (const [device, heading] of DEVICES) {
  lines.push(
    `#### ${heading}`,
    "",
    "<table>",
    "<thead>",
    '<tr><th rowspan="2" align="left">Page</th><th rowspan="2">Perf</th><th rowspan="2">A11y</th><th rowspan="2">Best Prac</th><th colspan="5">Performance Metrics</th></tr>',
    '<tr><th><abbr title="Largest Contentful Paint">LCP</abbr></th><th><abbr title="Total Blocking Time">TBT</abbr></th><th><abbr title="Cumulative Layout Shift">CLS</abbr></th><th><abbr title="First Contentful Paint">FCP</abbr></th><th><abbr title="Speed Index">SI</abbr></th></tr>',
    "</thead>",
    "<tbody>",
  );
  for (const [market, marketLabel] of MARKETS) {
    const run = runs[`${market}-${device}`];
    lines.push(`<tr><td colspan="9" align="left"><b>${escapeHtml(marketLabel)}</b></td></tr>`);
    for (const page of pages) {
      const result = run?.pages?.[page.slug];
      const failed = !run || run.status !== "ok" || !result || result.error;
      const pageCell = `<td><a href="${escapeHtml(`${baseUrl}/${market}${page.path}`)}">${escapeHtml(page.label)}</a></td>`;
      if (failed) {
        const reason =
          typeof result?.reason === "string" && /^[A-Za-z0-9_:.-]{1,40}$/.test(result.reason)
            ? ` · ${escapeHtml(result.reason)}`
            : "";
        lines.push(`<tr>${pageCell}<td colspan="8">⚠️ failed${reason}</td></tr>`);
        continue;
      }
      const cells = [
        scoreCell(result.performance),
        scoreCell(result.accessibility),
        scoreCell(result.bestPractices),
        ...METRICS.map(([metric]) => metricCell(result.metrics?.[metric])),
      ];
      lines.push(`<tr>${pageCell}${cells.map((cell) => `<td align="right">${cell}</td>`).join("")}</tr>`);
    }
  }
  lines.push("</tbody>", "</table>", "");
}

lines.push(
  "<sub>LCP Largest Contentful Paint · TBT Total Blocking Time · CLS Cumulative Layout Shift · FCP First Contentful Paint · SI Speed Index · Dots = Lighthouse score band</sub>",
  "",
);
lines.push(`[Full reports](${runUrl}) · Scores can vary a few points between runs`);
process.stdout.write(`${lines.join("\n")}\n`);

if (!scores && hostUp === "true") process.exitCode = 1;
