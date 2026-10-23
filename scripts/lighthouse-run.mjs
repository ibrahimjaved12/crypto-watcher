// Lighthouse for the logged-in pages of both markets on mobile and desktop (#463). Copied next to its own
// node_modules in $RUNNER_TEMP/lh by the workflow, so the bare imports resolve there and nothing is installed into
// the repo. Signs in once through the real form with the dedicated test account, reads the runner badge, then for
// each market selects it and audits every page on both devices in the same Chrome. Every run starts from a cold
// HTTP cache and cleared service workers/Cache Storage (Lighthouse's storage reset); localStorage is kept, so the
// login and the market stay. Every expected failure is written to scores.json with exit code 0; only an
// unexpected exception crashes. Never logs URLs with query strings, the email, the password or tokens.
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import * as chromeLauncher from "chrome-launcher";
import lighthouse from "lighthouse";
import desktopConfig from "lighthouse/core/config/desktop-config.js";
import puppeteer from "puppeteer-core";

const env = (name) => process.env[name] ?? "";
const BASE_URL = env("BASE_URL").replace(/\/+$/, "");
const OUT_DIR = env("OUT_DIR");
const PAGES = env("PAGES")
  .split("\n")
  .map((line) => line.trim())
  .filter(Boolean)
  .map((line) => {
    const [slug = "", label = "", path = ""] = line.split("|").map((part) => part.trim());
    return { slug, label, path };
  })
  .filter((page) => page.slug && page.path);
const WAIT_MS = 30_000;
const MARKETS = ["crypto", "forex"];
const DEVICES = ["mobile", "desktop"];
const METRICS = [
  ["largestContentfulPaint", "largest-contentful-paint"],
  ["totalBlockingTime", "total-blocking-time"],
  ["cumulativeLayoutShift", "cumulative-layout-shift"],
  ["firstContentfulPaint", "first-contentful-paint"],
  ["speedIndex", "speed-index"],
];
const startedAt = Date.now();
let runner;

const secondsSince = (from) => Math.round((Date.now() - from) / 100) / 10;

const score = (category) =>
  category && typeof category.score === "number" ? Math.round(category.score * 100) : null;

async function writeScores(result) {
  await mkdir(OUT_DIR, { recursive: true });
  await writeFile(
    join(OUT_DIR, "scores.json"),
    `${JSON.stringify(
      {
        status: result.status,
        ...(runner ? { runner } : {}),
        totalSeconds: secondsSince(startedAt),
        runs: result.runs ?? {},
      },
      null,
      2,
    )}\n`,
  );
}

async function main() {
  if (!BASE_URL || !OUT_DIR) throw new Error("BASE_URL and OUT_DIR are required");
  const email = env("LH_EMAIL");
  const password = env("LH_PASSWORD");
  if (!email || !password) {
    await writeScores({ status: "no-credentials" });
    console.log("no credentials");
    return;
  }

  const userDataDir = await mkdtemp(join(tmpdir(), "lh-chrome-"));
  let chrome;
  let browser;
  try {
    chrome = await chromeLauncher.launch({
      chromeFlags: ["--headless=new", "--no-sandbox", "--disable-gpu"],
      userDataDir,
    });
    browser = await puppeteer.connect({ browserURL: `http://127.0.0.1:${chrome.port}` });
    const page = await browser.newPage();

    try {
      await page.goto(`${BASE_URL}/auth`, { waitUntil: "domcontentloaded", timeout: 60_000 });
      await page.waitForSelector("#signin-email", { timeout: WAIT_MS });
      await page.type("#signin-email", email);
      await page.type("#signin-password", password);
      await page.click('form:has(#signin-email) button[type="submit"]');
      await page.waitForFunction(() => location.pathname === "/dashboard", { timeout: WAIT_MS });
    } catch {
      await writeScores({ status: "login-failed" });
      console.log("login failed");
      return;
    }

    try {
      await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded", timeout: 60_000 });
      await page.waitForSelector('nav[aria-label="Main"]', { timeout: WAIT_MS });
    } catch {
      await writeScores({ status: "no-access" });
      console.log("no access");
      return;
    }

    // The badge is rendered by the app shell only when a runner is configured; otherwise it stays "unknown".
    runner = "unknown";
    try {
      const label = await page
        .waitForSelector('button[aria-label^="Runner "]', { timeout: 20_000 })
        .then((el) => el.evaluate((node) => node.getAttribute("aria-label") ?? ""));
      runner = /^runner offline/i.test(label) ? "offline" : /^runner online/i.test(label) ? "online" : "unknown";
    } catch {
      runner = "unknown";
    }
    console.log(`runner ${runner}`);
    await page.close();

    const runs = {};
    for (const market of MARKETS) {
      const marketPage = await browser.newPage();
      let available = true;
      try {
        await marketPage.goto(`${BASE_URL}/auth`, { waitUntil: "domcontentloaded", timeout: 60_000 });
        await marketPage.evaluate((m) => localStorage.setItem("cw.market", m), market);
        await marketPage.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded", timeout: 60_000 });
        await marketPage.waitForFunction(
          (m) => document.documentElement.dataset.market === m,
          { timeout: WAIT_MS },
          market,
        );
      } catch {
        available = false;
      }
      await marketPage.close().catch(() => {});
      if (!available) {
        for (const device of DEVICES) runs[`${market}-${device}`] = { status: "market-unavailable", pages: {} };
        console.log(`market ${market} unavailable`);
        continue;
      }

      for (const device of DEVICES) {
        const pages = {};
        for (const { slug, path } of PAGES) {
          const started = Date.now();
          let callFailed = false;
          let errorName = null;
          const result = await lighthouse(
            `${BASE_URL}${path}`,
            {
              port: chrome.port,
              output: ["json", "html"],
              logLevel: "error",
              onlyCategories: ["performance", "accessibility", "best-practices"],
              disableFullPageScreenshot: true,
              // Settings shows the account email, so it gets no final screenshot.
              skipAudits: slug === "settings" ? ["screenshot-thumbnails", "final-screenshot"] : ["screenshot-thumbnails"],
              maxWaitForLoad: 60_000,
            },
            device === "desktop" ? desktopConfig : undefined,
          ).catch((error) => {
            callFailed = true;
            errorName = typeof error?.name === "string" ? error.name : null;
            return undefined;
          });
          const seconds = secondsSince(started);
          const lhr = result?.lhr;
          let finalPath = null;
          try {
            finalPath = lhr ? new URL(lhr.finalDisplayedUrl).pathname : null;
          } catch {
            finalPath = null;
          }
          if (!lhr || lhr.runtimeError || finalPath !== path) {
            const runtimeErrorCode = lhr?.runtimeError?.code;
            let reason;
            if (runtimeErrorCode !== undefined && runtimeErrorCode !== null) {
              reason = runtimeErrorCode;
            } else if (finalPath !== null && finalPath !== path) {
              reason = "redirected";
            } else if (callFailed) {
              reason = errorName ? `error:${errorName}` : "error";
            } else {
              reason = "no-result";
            }
            pages[slug] = { error: true, seconds, reason };
            console.log(`${market} ${device} ${slug} ${seconds}s failed (${reason})`);
            continue;
          }
          const [json, html] = Array.isArray(result.report) ? result.report : [result.report, ""];
          const base = join(OUT_DIR, `${market}-${device}-${slug}`);
          await writeFile(`${base}.json`, json);
          if (html) await writeFile(`${base}.html`, html);
          if (slug !== "settings") {
            const shot = lhr.audits["final-screenshot"]?.details?.data;
            const match = typeof shot === "string" ? /^data:image\/jpeg;base64,(.+)$/.exec(shot) : null;
            if (match) await writeFile(`${base}.jpg`, Buffer.from(match[1], "base64"));
          }
          const scores = {
            performance: score(lhr.categories.performance),
            accessibility: score(lhr.categories.accessibility),
            bestPractices: score(lhr.categories["best-practices"]),
          };
          const metrics = Object.fromEntries(
            METRICS.map(([key, id]) => {
              const audit = lhr.audits[id];
              return [
                key,
                {
                  text:
                    typeof audit?.displayValue === "string"
                      ? audit.displayValue.replace(/\u00a0/g, " ")
                      : null,
                  score:
                    typeof audit?.score === "number" ? Math.round(audit.score * 100) / 100 : null,
                },
              ];
            }),
          );
          pages[slug] = { ...scores, metrics, seconds };
          console.log(
            `${market} ${device} ${slug} ${seconds}s perf=${scores.performance} a11y=${scores.accessibility} bp=${scores.bestPractices} lcp=${metrics.largestContentfulPaint.text ?? "?"} tbt=${metrics.totalBlockingTime.text ?? "?"} cls=${metrics.cumulativeLayoutShift.text ?? "?"}`,
          );
        }
        runs[`${market}-${device}`] = { status: "ok", pages };
      }
    }
    await writeScores({ status: "ok", runs });
    console.log(`total ${secondsSince(startedAt)}s`);
  } finally {
    await browser?.disconnect().catch(() => {});
    try {
      await chrome?.kill();
    } catch {
      // already gone
    }
    await rm(userDataDir, { recursive: true, force: true }).catch(() => {});
  }
}

await main();
