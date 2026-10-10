/**
 * Display translation layer (P18). Backend values (reasons, statuses, strategy ids, enums) are
 * canonical contracts and are never renamed; this module only turns them into plain language for
 * the UI. Every function is total: unknown input falls back to a humanised string, never throws.
 * Pure TypeScript (no React) so tests can transpile it directly.
 */

export type Level = "ok" | "wait" | "problem";
export type Reason = { short: string; help?: string | undefined; level?: Level | undefined };

const cap = (text: string) => (text ? `${text.charAt(0).toUpperCase()}${text.slice(1)}` : text);

/** "some_new_reason" -> "Some new reason"; "STALE_LAST_TRADE" -> "Stale last trade". */
export function humanizeToken(raw: string | null | undefined): string {
  try {
    const text = String(raw ?? "").trim();
    if (!text) return "No details";
    if (/\s/.test(text)) return cap(text.replace(/_/g, " "));
    return cap(
      text
        .replace(/[_-]+/g, " ")
        .replace(/([a-z])([A-Z])/g, "$1 $2")
        .toLowerCase()
        .trim(),
    );
  } catch {
    return "Unknown";
  }
}

const EXACT: Record<string, Reason> = {
  // generic / forward run statuses
  ok: { short: "OK", level: "ok" },
  already_done: { short: "Already done for this period", level: "ok" },
  skipped_stale: {
    short:
      "Skipped: market data was too old to trust, so no trades were simulated (safe behaviour)",
    help: "The signal engine refuses to simulate trades on gappy or delayed candles. It retries on the next hourly run.",
    level: "wait",
  },
  funding_unavailable: {
    short: "Funding rates could not be fetched, so days cannot be scored yet",
    help: "Futures positions pay or receive funding every few hours. Without those rates the result of a day is unknown, so it stays open until the rates can be fetched.",
    level: "wait",
  },
  "collector missing": {
    short: "Live data feed has not started for this pair",
    help: "The market-data collector has not stored any health record for this symbol yet.",
    level: "wait",
  },
  "no candles": {
    short: "No price candles stored yet",
    help: "The collector needs time to fill the price history before anything can be calculated.",
    level: "wait",
  },
  "last completed minute missing": {
    short: "The latest one-minute candle is not stored yet",
    level: "wait",
  },
  // monitoring run statuses (monitor/engine.server.ts)
  success: { short: "Completed", level: "ok" },
  failed: { short: "Failed", level: "problem" },
  partial: { short: "Partly completed", level: "wait" },
  skipped: { short: "Skipped (nothing to check)", level: "wait" },
  // analysis failure categories (analysis.contract.ts)
  provider_timeout: { short: "The exchange took too long to answer", level: "problem" },
  provider_unavailable: { short: "The exchange could not be reached", level: "problem" },
  invalid_or_stale_data: {
    short: "The exchange returned data that was invalid or too old",
    level: "problem",
  },
  invalid_data: { short: "The exchange returned invalid data", level: "problem" },
  stale_data: { short: "The latest market data is too old to use", level: "wait" },
  insufficient_history: {
    short: "Not enough past candles yet for this timeframe",
    help: "Indicators such as the 200-candle average need a long history. They appear once enough candles are stored.",
    level: "wait",
  },
  // TA result / rolling window statuses
  insufficient: { short: "Not enough candles yet", level: "wait" },
  unavailable: { short: "Unavailable", level: "problem" },
  stale: { short: "Data too old", level: "wait" },
  // collector / movement / freshness states
  live: { short: "Live", level: "ok" },
  recovering: { short: "Catching up", level: "wait" },
  warming: { short: "Warming up", level: "wait" },
  fresh: { short: "Up to date", level: "ok" },
  delayed: { short: "Delayed", level: "wait" },
  ready: { short: "Ready", level: "ok" },
  // analysis baseline statuses
  eligible: { short: "Alert condition met", level: "ok" },
  cooldown: { short: "On cooldown", level: "wait" },
  below_threshold: { short: "Below your alert threshold", level: "ok" },
  already_processed: { short: "Already checked by the monitor", level: "ok" },
  disabled: { short: "Monitoring paused", level: "wait" },
  baseline_required: { short: "No saved starting price yet", level: "wait" },
  baseline_reset_required: { short: "Starting price must be reset", level: "wait" },
  invalid_state: { short: "Saved starting price is inconsistent", level: "problem" },
  // movement engine reasons
  stale_last_trade: { short: "No recent trade for this pair", level: "wait" },
  warming_insufficient_live_history: {
    short: "Still collecting live history",
    level: "wait",
  },
  source_recovering: { short: "Data feed is catching up", level: "wait" },
  source_stale: { short: "Data feed is lagging", level: "wait" },
  source_unavailable: { short: "Data feed is offline", level: "problem" },
  missing_symbol_input: { short: "No data for this pair in the latest check", level: "wait" },
  // trend track
  "not every track is finalised": {
    short: "Some portfolios are not scored up to the latest day yet",
    level: "wait",
  },
  "daily bars not yet complete for every symbol": {
    short: "Yesterday’s daily candle is not available for every coin yet",
    level: "wait",
  },
  "incomplete startup: complete frozen universe history required": {
    short: "Waiting for the full daily history of all six coins before starting",
    level: "wait",
  },
  ta_signals_futures_provenance_check: {
    short:
      "A saved signal was rejected because its data source details were incomplete (internal consistency check)",
    level: "problem",
  },
  "collector mode disabled": { short: "Live data collector is switched off", level: "wait" },
  "binance stream is stale": { short: "The Binance live stream stopped sending data", level: "problem" },
};

type Pattern = [RegExp, (match: RegExpMatchArray) => Reason];

const PATTERNS: Pattern[] = [
  [/^skipped_stale\b/i, () => EXACT["skipped_stale"]!],
  [
    /^funding_unavailable\s*:?\s*(.*)$/i,
    (m) => ({
      ...EXACT["funding_unavailable"]!,
      short: `${EXACT["funding_unavailable"]!.short}${m[1] ? ` (${coins(m[1])})` : ""}`,
    }),
  ],
  [
    /^collector (missing|LIVE|RECOVERING|STALE|UNAVAILABLE)$/i,
    (m) => {
      const state = m[1]!.toLowerCase();
      if (state === "missing") return EXACT["collector missing"]!;
      if (state === "recovering") return { short: "Live data feed is catching up", level: "wait" };
      if (state === "stale") return { short: "Live data feed is lagging", level: "wait" };
      if (state === "unavailable") return { short: "Live data feed is offline", level: "problem" };
      return { short: "Live data feed is running", level: "ok" };
    },
  ],
  [
    /^(\d+) gap\(s\) in candle history$/i,
    (m) => ({ short: `${m[1]} gap(s) in the stored price history`, level: "wait" }),
  ],
  [
    /catch-up gap exceeds the available (\d+)-candle history/i,
    () => ({
      short:
        "Technical analysis could not catch up: the gap since the last run is longer than the stored history. It will recover as new candles arrive.",
      level: "wait",
    }),
  ],
  [
    /history is still warming up \((\d+)\/(\d+) completed candles\)/i,
    (m) => ({
      short: `Still collecting history (${m[1]} of ${m[2]} candles needed). Retries automatically.`,
      level: "wait",
    }),
  ],
  [
    /insufficient_history for (\d+) decisions/i,
    (m) => ({ short: `${m[1]} decision(s) skipped: not enough past candles yet`, level: "wait" }),
  ],
  [/^rate:trailing-30d$/i, () => ({ short: "Random baseline paced at the strategy’s last-30-day rate", level: "ok" })],
  [
    /_provenance_check/i,
    () => ({
      short:
        "A saved record was rejected because its data source details were incomplete (internal consistency check)",
      level: "problem",
    }),
  ],
  [
    /python ta result unavailable or mismatched/i,
    () => ({ short: "The indicator calculation returned an unexpected result; it was not saved", level: "problem" }),
  ],
  [
    /expected completed ta candle is not available/i,
    () => ({ short: "The newest finished candle was not available yet", level: "wait" }),
  ],
  [
    /saved ta checkpoint is invalid or ahead of exchange time/i,
    () => ({ short: "The saved analysis position is ahead of the exchange clock", level: "problem" }),
  ],
  [
    /monitor lease (unavailable|renewal failed|release failed)/i,
    () => ({ short: "Another check was running at the same time", level: "wait" }),
  ],
  [
    /daily kline fetch failed or history incomplete/i,
    () => ({ short: "Daily candles could not be downloaded", level: "problem" }),
  ],
  [
    /forward daily bar conflict/i,
    () => ({ short: "A stored daily candle differs from Binance (kept, not overwritten)", level: "problem" }),
  ],
  [
    /funding settlement schedule unavailable/i,
    () => ({ short: "Funding schedule could not be fetched", level: "wait" }),
  ],
  [/timeout|timed out/i, () => ({ short: "A request took too long and was stopped", level: "problem" })],
];

function coins(list: string): string {
  return list
    .split(/[,\s]+/)
    .filter(Boolean)
    .map((symbol) => symbol.replace(/USDT$/, ""))
    .join(", ");
}

/** One machine string -> plain language. Total: never throws, never returns an empty `short`. */
export function humanizeReason(raw: string | null | undefined): Reason {
  try {
    const text = String(raw ?? "").trim();
    if (!text) return { short: "No details" };
    const exact = EXACT[text] ?? EXACT[text.toLowerCase()];
    if (exact) return exact;
    for (const [pattern, build] of PATTERNS) {
      const match = text.match(pattern);
      if (match) return build(match);
    }
    return { short: humanizeToken(text) };
  } catch {
    return { short: "Unknown status" };
  }
}

const RANK: Record<Level, number> = { ok: 0, wait: 1, problem: 2 };

export type ReasonSummary = Reason & { items: string[]; raw: string };

/**
 * A composite reason ("SOLUSDT: collector missing; SOLUSDT: no candles", or monitor errors joined
 * by " | ") -> deduplicated plain items, grouped by message with the affected coins listed.
 */
export function summarizeReasons(raw: string | null | undefined): ReasonSummary {
  const text = String(raw ?? "").trim();
  if (!text) return { short: "No details", items: [], raw: text };
  try {
    const groups = new Map<string, { reason: Reason; symbols: string[] }>();
    for (const part of text.split(/;\s*|\s+\|\s+/)) {
      const piece = part.trim();
      if (!piece) continue;
      const prefixed = /^([A-Z0-9]{2,20}USDT)\s*:\s*(.+)$/.exec(piece);
      const symbol = prefixed?.[1];
      const reason = humanizeReason(prefixed ? prefixed[2] : piece);
      const group = groups.get(reason.short) ?? { reason, symbols: [] };
      if (symbol && !group.symbols.includes(symbol)) group.symbols.push(symbol);
      groups.set(reason.short, group);
    }
    const values = [...groups.values()];
    const items = values.map(({ reason, symbols }) =>
      symbols.length ? `${reason.short} (${coins(symbols.join(","))})` : reason.short,
    );
    const level = values.reduce<Level | undefined>(
      (worst, { reason }) =>
        reason.level && (!worst || RANK[reason.level] > RANK[worst]) ? reason.level : worst,
      undefined,
    );
    const help = values.find(({ reason }) => reason.help)?.reason.help;
    return {
      short: items[0] ?? "No details",
      ...(help ? { help } : {}),
      ...(level ? { level } : {}),
      items,
      raw: text,
    };
  } catch {
    return { short: humanizeToken(text), items: [humanizeToken(text)], raw: text };
  }
}

// ---------------------------------------------------------------- strategies

const TA_STRATEGIES: Record<string, { name: string; blurb: string }> = {
  ema_cross_20_50: {
    name: "EMA 20/50 crossover",
    blurb: "Long when the 20-candle average crosses above the 50-candle average, short when it crosses below.",
  },
  ema_cross_50_200: {
    name: "EMA 50/200 crossover",
    blurb: "Long when the 50-candle average crosses above the 200-candle average, short when it crosses below.",
  },
  rsi_14_reversion: {
    name: "RSI bounce (30/70)",
    blurb: "Long when RSI(14) climbs back above 30, short when it falls back below 70.",
  },
  macd_12_26_9: {
    name: "MACD crossover",
    blurb: "Long when the MACD(12,26) line crosses above its 9-candle signal line, short when below.",
  },
  bollinger_20_2_reversion: {
    name: "Bollinger band bounce",
    blurb: "Long when price closes back inside the lower 20/2 band, short when back inside the upper band.",
  },
  keltner_breakout_20_14_2: {
    name: "Keltner channel breakout",
    blurb: "Long when price closes above the 20-EMA + 2×ATR channel, short when it closes below it.",
  },
};

const TREND_TRACKS: Record<string, { name: string; blurb: string }> = {
  ens_ls_25: {
    name: "Trend ensemble, long/short, 25% vol",
    blurb: "Nine Donchian breakout lookbacks (5–360 days) voting, long or short, sized to 25% yearly volatility.",
  },
  ens_lo_25: {
    name: "Trend ensemble, long only, 25% vol",
    blurb: "Same trend ensemble, but only ever long (flat instead of short), 25% volatility target.",
  },
  ens_ls_15: {
    name: "Trend ensemble, long/short, 15% vol",
    blurb: "The trend ensemble with a calmer 15% yearly volatility target.",
  },
  ens_ls_25_sub3: {
    name: "Trend ensemble (3 lookbacks), 25% vol",
    blurb: "A slimmer ensemble using only the 20, 60 and 150-day breakouts, long or short, 25% volatility target.",
  },
  tsmom_7: {
    name: "7-day momentum, 25% vol",
    blurb: "Long if the last 7 days went up, short if they went down; 25% volatility target.",
  },
  tsmom_14: {
    name: "14-day momentum, 25% vol",
    blurb: "Long if the last 14 days went up, short if they went down; 25% volatility target.",
  },
  tsmom_28: {
    name: "28-day momentum, 25% vol",
    blurb: "Long if the last 28 days went up, short if they went down; 25% volatility target.",
  },
  ens_ls_25_mfilter: {
    name: "Trend ensemble + moving-average filter",
    blurb: "The 25% trend ensemble, trading only in the direction most 20/50/100/200-day averages agree with.",
  },
  ens_ls_25_rvfilter: {
    name: "Trend ensemble + volatility filter",
    blurb: "The 25% trend ensemble, standing aside when recent volatility is unusually high.",
  },
  ew_long: {
    name: "Hold all coins equally",
    blurb: "Baseline: always long every coin with equal weight. Anything worth using should beat this.",
  },
};

function frameLabel(minutes: number): string {
  return minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}

/** Strategy / track id -> human name and one-line explanation. Unknown ids are humanised. */
export function strategyLabel(id: string | null | undefined): { name: string; blurb: string } {
  try {
    const text = String(id ?? "").trim();
    if (!text) return { name: "Unknown strategy", blurb: "" };
    if (TREND_TRACKS[text]) return TREND_TRACKS[text]!;
    const ta = /^(placebo-v1:)?([a-z0-9_]+):(\d+)$/.exec(text);
    if (ta) {
      const base = TA_STRATEGIES[ta[2]!] ?? { name: humanizeToken(ta[2]), blurb: "" };
      const frame = frameLabel(Number(ta[3]));
      return ta[1]
        ? {
            name: `Random baseline for ${base.name} · ${frame}`,
            blurb: "Random entries at the same pace and with the same exits, to show what luck alone would do.",
          }
        : { name: `${base.name} · ${frame}`, blurb: `${base.blurb} On ${frame} candles.` };
    }
    if (TA_STRATEGIES[text]) return TA_STRATEGIES[text]!;
    const control = /^bh_vt_(std90|ewma60)_(\d+)$/.exec(text);
    if (control) {
      const estimator = control[1] === "std90" ? "90-day volatility" : "60-day smoothed volatility";
      return {
        name: `Hold all coins, ${control[2]}% vol`,
        blurb: `Baseline: always long every coin, sized to a ${control[2]}% yearly volatility target using ${estimator}.`,
      };
    }
    return { name: humanizeToken(text), blurb: "" };
  } catch {
    return { name: "Unknown strategy", blurb: "" };
  }
}

// ---------------------------------------------------------------- outcomes

const OUTCOMES: Record<string, { short: string; help: string; level: Level }> = {
  T: { short: "Target hit", help: "Price reached the take-profit level first.", level: "ok" },
  S: { short: "Stopped out", help: "Price reached the stop-loss level first.", level: "problem" },
  E: { short: "Expired", help: "Neither level was hit before the time limit; closed at market.", level: "wait" },
  L: { short: "Liquidated", help: "Leverage losses wiped out the margin before the stop.", level: "problem" },
  X: { short: "Unresolved", help: "Not enough data yet to decide the result.", level: "wait" },
  ambiguous: {
    short: "Unclear",
    help: "Target and stop were both touched inside the same candle; counted pessimistically.",
    level: "wait",
  },
};

export function outcomeLabel(code: string | null | undefined): { short: string; help: string; level: Level } {
  const text = String(code ?? "");
  return OUTCOMES[text] ?? { short: humanizeToken(text), help: "", level: "wait" };
}

// ---------------------------------------------------------------- health states

const COLLECTOR: Record<string, { short: string; level: Level }> = {
  LIVE: { short: "Live", level: "ok" },
  RECOVERING: { short: "Catching up", level: "wait" },
  WARMING: { short: "Warming up", level: "wait" },
  STALE: { short: "Lagging", level: "wait" },
  UNAVAILABLE: { short: "Offline", level: "problem" },
};

/** Collector / movement-engine state (LIVE, RECOVERING, STALE, UNAVAILABLE, WARMING). */
export function collectorLabel(status: string | null | undefined): { short: string; level: Level } {
  return COLLECTOR[String(status ?? "")] ?? { short: humanizeToken(status), level: "wait" };
}

const FRESHNESS: Record<string, { short: string; level: Level }> = {
  FRESH: { short: "Up to date", level: "ok" },
  DELAYED: { short: "Delayed", level: "wait" },
  STALE: { short: "Out of date", level: "problem" },
  UNAVAILABLE: { short: "No data", level: "problem" },
};

export function freshnessLabel(state: string | null | undefined): { short: string; level: Level } {
  return FRESHNESS[String(state ?? "")] ?? { short: humanizeToken(state), level: "wait" };
}

// ---------------------------------------------------------------- misc display helpers

const SOURCES: Record<string, string> = {
  "binance-usdm": "Binance futures",
  "okx-usdt-swap": "OKX futures",
  "kraken-futures": "Kraken futures",
};

export function sourceLabel(source: string | null | undefined): string {
  const text = String(source ?? "");
  return SOURCES[text] ?? (text ? humanizeToken(text) : "Unknown source");
}

/** "12 min ago" style relative time; exact time belongs in a tooltip. */
export function relativeTime(at: number | string | Date | null | undefined, now = Date.now()): string {
  if (at === null || at === undefined) return "—";
  const ms = at instanceof Date ? at.getTime() : typeof at === "number" ? at : Date.parse(at);
  if (!Number.isFinite(ms)) return "—";
  const diff = Math.round((now - ms) / 1000);
  const future = diff < 0;
  const s = Math.abs(diff);
  const text =
    s < 45
      ? "just now"
      : s < 3_600
        ? `${Math.round(s / 60)} min`
        : s < 86_400
          ? `${Math.round(s / 3_600)} h`
          : `${Math.round(s / 86_400)} d`;
  if (text === "just now") return text;
  return future ? `in ${text}` : `${text} ago`;
}

export const GLOSSARY: Record<string, { term: string; plain: string }> = {
  R: {
    term: "R (risk unit)",
    plain: "R = the amount risked on one trade (entry to stop). +1R means you won what you risked; −1R means you lost it. Costs are included.",
  },
  ATR: {
    term: "ATR (average true range)",
    plain: "How much price typically moves per candle. ATR % = ATR divided by price. Measures size of moves, not direction.",
  },
  RSI: {
    term: "RSI (relative strength index)",
    plain: "Momentum from 0 to 100. Below 30 is called oversold, above 70 overbought. Not a reversal signal by itself.",
  },
  EMA: {
    term: "EMA (exponential moving average)",
    plain: "An average price that weights recent candles more. Price above its EMA leans bullish, below leans bearish.",
  },
  funding: {
    term: "Funding",
    plain: "A small payment between long and short futures traders every few hours. It is a real cost (or income) of holding a position.",
  },
  placebo: {
    term: "Random baseline",
    plain: "Random entries at the same pace and with the same exits as the strategy. If a strategy can’t beat this, its results are probably luck.",
  },
  score: {
    term: "Indicator score",
    plain: "A ranking from −100 (indicators look bearish) to +100 (bullish). It is not a probability of winning or a price forecast.",
  },
  scoreBand: {
    term: "Score band",
    plain: "Groups trades by the strategy’s score when it fired, to check whether higher scores really did better. Not scored yet.",
  },
  turnover: {
    term: "Turnover per day",
    plain: "How much of the portfolio is traded each day. Higher turnover means more fees.",
  },
  gross: {
    term: "Gross exposure",
    plain: "Total size of all positions relative to the account (1.00 = 100% of equity, longs and shorts added).",
  },
  volTarget: {
    term: "Volatility target",
    plain: "Positions are sized so the portfolio swings by about this much per year. Calmer coins get bigger positions.",
  },
  prospective: {
    term: "Live days",
    plain: "Days whose positions were saved before that day’s close and scored afterwards. The trustworthy sample.",
  },
  reconstructed: {
    term: "Back-filled days",
    plain: "Days replayed from history after the fact. Useful for context but less trustworthy than live days.",
  },
  mde: {
    term: "Minimum detectable edge",
    plain: "The smallest average daily gain that the number of days so far could tell apart from zero. More days shrink it.",
  },
  rr: {
    term: "Reward : risk",
    plain: "Target distance divided by stop distance. 3/2 means the target is 1.5× as far as the stop.",
  },
};
