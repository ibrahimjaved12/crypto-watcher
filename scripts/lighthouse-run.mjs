// Lighthouse for the logged-in pages of one market on one device (#463). Copied next to its own node_modules in
// $RUNNER_TEMP/lh by the workflow, so the bare imports resolve there and nothing is installed into the repo.
// Signs in through the real form with the dedicated test account, selects the market, then audits each page in
// the same Chrome (storage kept). Every expected failure is written to scores.json with exit code 0; only an
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
const MARKET = env("MARKET");
const DEVICE = env("DEVICE");
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

const score = (category) =>
  category && typeof category.score === "number" ? Math.round(category.score * 100) : null;

async function writeScores(result) {
  await mkdir(OUT_DIR, { recursive: true });
  await writeFile(
    join(OUT_DIR, "scores.json"),
    `${JSON.stringify({ market: MARKET, device: DEVICE, ...result }, null, 2)}\n`,
  );
}

async function main() {
  if (!BASE_URL || !OUT_DIR || !["crypto", "forex"].includes(MARKET) || !["mobile", "desktop"].includes(DEVICE))
    throw new Error("BASE_URL, OUT_DIR, MARKET (crypto|forex) and DEVICE (mobile|desktop) are required");
  const email = env("LH_EMAIL");
  const password = env("LH_PASSWORD");
  if (!email || !password) {
    await writeScores({ status: "no-credentials", pages: {} });
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

    await page.goto(`${BASE_URL}/auth`, { waitUntil: "networkidle2", timeout: 60_000 });
    await page.waitForSelector("#signin-email", { timeout: WAIT_MS });
    await page.type("#signin-email", email);
    await page.type("#signin-password", password);
    await page.click('form:has(#signin-email) button[type="submit"]');
    try {
      await page.waitForFunction(() => location.pathname === "/dashboard", { timeout: WAIT_MS });
    } catch {
      await writeScores({ status: "login-failed", pages: {} });
      console.log("login failed");
      return;
    }

    await page.evaluate((market) => localStorage.setItem("cw.market", market), MARKET);
    await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "networkidle2", timeout: 60_000 });
    try {
      await page.waitForFunction(
        (market) => document.documentElement.dataset.market === market,
        { timeout: WAIT_MS },
        MARKET,
      );
    } catch {
      await writeScores({ status: "market-unavailable", pages: {} });
      console.log(`market ${MARKET} unavailable`);
      return;
    }
    await page.close();

    const pages = {};
    for (const { slug, path } of PAGES) {
      const started = Date.now();
      const result = await lighthouse(
        `${BASE_URL}${path}`,
        {
          port: chrome.port,
          output: ["json", "html"],
          logLevel: "error",
          onlyCategories: ["performance", "accessibility", "best-practices"],
          disableStorageReset: true,
          disableFullPageScreenshot: true,
          skipAudits: ["screenshot-thumbnails", "final-screenshot"],
          maxWaitForLoad: 60_000,
        },
        DEVICE === "desktop" ? desktopConfig : undefined,
      ).catch(() => undefined);
      const seconds = Math.round((Date.now() - started) / 100) / 10;
      const lhr = result?.lhr;
      let finalPath = null;
      try {
        finalPath = lhr ? new URL(lhr.finalDisplayedUrl).pathname : null;
      } catch {
        finalPath = null;
      }
      if (!lhr || lhr.runtimeError || finalPath !== path) {
        pages[slug] = { error: true, seconds };
        console.log(`${slug} ${seconds}s failed`);
        continue;
      }
      const [json, html] = Array.isArray(result.report) ? result.report : [result.report, ""];
      const base = join(OUT_DIR, `${MARKET}-${DEVICE}-${slug}`);
      await writeFile(`${base}.json`, json);
      if (html) await writeFile(`${base}.html`, html);
      const scores = {
        performance: score(lhr.categories.performance),
        accessibility: score(lhr.categories.accessibility),
        bestPractices: score(lhr.categories["best-practices"]),
      };
      pages[slug] = { ...scores, seconds };
      console.log(
        `${slug} ${seconds}s perf=${scores.performance} a11y=${scores.accessibility} bp=${scores.bestPractices}`,
      );
    }
    await writeScores({ status: "ok", pages });
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
