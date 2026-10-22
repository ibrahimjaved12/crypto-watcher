/**
 * Forex paper account view models (#454, pure). The forex wallet (`python/market_analysis/fx/wallet.py`,
 * `fx-wallet-v1`) writes the crypto ledger shape into the same `paper_ledger`, but its state is different: sizes
 * are base `units` with the broker's `contract_size` (lots = units / contract size), there is no wallet mark, swap is
 * a `funding` line with `kind: "swap"`, and a stop-out is a `liquidation` line. Everything here is deterministic
 * mapping and formatting (callers pass `nowMs`); money is integers in 10^-8 USD, prices exact text or 10^8 ints.
 *
 * Every reason code, close reason and ledger type has a short label and an icon key; the page maps the key to an
 * icon, so a colour is never the only signal.
 */
import { THRESHOLDS } from "../forward/mvp-check";
import {
  E8,
  dateTimeText,
  e8ToUsdt,
  formatDuration,
  formatPercent,
  formatUsdt,
  parseExact,
  signedE8,
  signedUsdt,
  type LedgerRow,
  type Signed,
  type Tone,
} from "./paper-account";

/** A forex run older than this is stale: the hourly pass writes one per hour, so two hours = a missed run. */
export const FX_STALE_AFTER_MS = THRESHOLDS.runPassWithinMs;
/** A published forex quote older than this is shown as delayed when it values an open position. */
export const FX_QUOTE_STALE_MS = 10 * 60_000;

export type FxIconKey =
  | "lot"
  | "exposure"
  | "positions"
  | "margin"
  | "contract"
  | "blocked"
  | "leverage"
  | "unknown"
  | "target"
  | "stop"
  | "time"
  | "stop-out"
  | "unclear"
  | "early"
  | "open-fee"
  | "close-fee"
  | "result"
  | "swap"
  | "rejected";

type Entry = { label: string; help: string; icon: FxIconKey };

// ---------------------------------------------------------------- rejection reasons

/** Every reason `fx/wallet.py` writes on a `rejected` line. */
export const FX_REJECTION_CODES = [
  "below_min_lot",
  "cap:total_exposure",
  "cap:max_positions",
  "insufficient_margin",
  "no_contract",
  "not_tradeable",
  "no_admissible_leverage",
] as const;
export type FxRejectionCode = (typeof FX_REJECTION_CODES)[number];

export const FX_REJECTIONS: Record<FxRejectionCode, Entry> = {
  below_min_lot: {
    label: "Below minimum lot",
    help: "1 % risk over this stop distance is smaller than the broker's minimum lot, so no size fits.",
    icon: "lot",
  },
  "cap:total_exposure": {
    label: "Exposure cap",
    help: "Opening it would take total open notional above the cap (10 × balance by default).",
    icon: "exposure",
  },
  "cap:max_positions": {
    label: "Position cap",
    help: "The account already holds the maximum number of open positions.",
    icon: "positions",
  },
  insufficient_margin: {
    label: "Not enough margin",
    help: "Free margin could not cover this position's margin plus its opening commission.",
    icon: "margin",
  },
  no_contract: {
    label: "No contract specs",
    help: "The broker's contract size, lot step and minimum lot for this symbol were not available.",
    icon: "contract",
  },
  not_tradeable: {
    label: "Not tradeable",
    help: "The setup had no valid entry (for example the market was closed or a candle was missing).",
    icon: "blocked",
  },
  no_admissible_leverage: {
    label: "No safe leverage",
    help: "No leverage keeps the stop-out at least two stop distances away from the entry.",
    icon: "leverage",
  },
};

const humanize = (code: string) => (code ? code.replace(/^cap:/, "").replaceAll("_", " ") : "Unknown reason");

export function fxRejection(code: string | null | undefined): Entry & { code: string } {
  const known = FX_REJECTIONS[code as FxRejectionCode] as Entry | undefined;
  return known
    ? { ...known, code: code ?? "" }
    : { label: humanize(code ?? ""), help: "A reason this page does not know yet.", icon: "unknown", code: code ?? "" };
}

// ---------------------------------------------------------------- close reasons

export type FxCloseReason = "target" | "stop" | "time" | "stop_out" | "ambiguous" | "unresolved";
export const FX_CLOSE_REASONS: Record<FxCloseReason, Entry & { tone: "good" | "bad" | "neutral" }> = {
  target: { label: "Target", help: "The target was reached first.", icon: "target", tone: "good" },
  stop: { label: "Stop", help: "The stop was reached first.", icon: "stop", tone: "bad" },
  time: { label: "Time", help: "Neither level was reached before the horizon ran out.", icon: "time", tone: "neutral" },
  stop_out: {
    label: "Stop-out",
    help: "The broker's margin stop-out closed it before the stop (isolated per position here; conservative).",
    icon: "stop-out",
    tone: "bad",
  },
  ambiguous: {
    label: "Unclear",
    help: "Stop and target were both inside one candle; it is counted the worse way.",
    icon: "unclear",
    tone: "neutral",
  },
  unresolved: { label: "Closed early", help: "Closed without a target, stop or time limit.", icon: "early", tone: "neutral" },
};
const OUTCOME_TO_REASON: Record<string, FxCloseReason> = { T: "target", S: "stop", E: "time", ambiguous: "ambiguous" };

export function fxCloseReason(line: Pick<LedgerRow, "type" | "payload">): FxCloseReason {
  if (line.type === "liquidation") return "stop_out";
  const outcome = line.payload?.["outcome"];
  return OUTCOME_TO_REASON[typeof outcome === "string" ? outcome : ""] ?? "unresolved";
}

// ---------------------------------------------------------------- ledger types

/** Every line type the forex wallet writes. */
export const FX_LEDGER_TYPES = ["open_fee", "pnl", "close_fee", "funding", "liquidation", "rejected"] as const;
export type FxLedgerType = (typeof FX_LEDGER_TYPES)[number];
export const FX_LEDGER: Record<FxLedgerType, Entry> = {
  open_fee: { label: "Commission · open", help: "Broker commission per lot on the opening fill.", icon: "open-fee" },
  pnl: { label: "Trade result", help: "Price result of the closed position, before costs.", icon: "result" },
  close_fee: { label: "Commission · close", help: "Broker commission per lot on the closing fill.", icon: "close-fee" },
  funding: { label: "Swap", help: "Overnight swap at the daily rollover (triple on the triple day).", icon: "swap" },
  liquidation: { label: "Stop-out", help: "Loss when the margin stop-out closed the position.", icon: "stop-out" },
  rejected: { label: "Rejected", help: "A setup the account could not open; no money moved.", icon: "rejected" },
};
const MONEY = new Set<string>(["open_fee", "pnl", "close_fee", "funding", "liquidation"]);

export function fxLedgerType(type: string): Entry {
  return (
    (FX_LEDGER[type as FxLedgerType] as Entry | undefined) ?? {
      label: type.replaceAll("_", " "),
      help: "A line type this page does not know yet.",
      icon: "unknown",
    }
  );
}

// ---------------------------------------------------------------- formatting

/** Lots = units / contract size (both exact text); null when either is missing or the size is not positive. */
export function lotsOf(units: unknown, contractSize: unknown): number | null {
  const u = parseExact(units);
  const size = parseExact(contractSize);
  if (u === null || size === null || size <= 0) return null;
  return u / size;
}

/** Lots with at least two decimals (the usual 0.01 step) and up to three: "0.10", "1.25", "0.015". */
export function formatLots(lots: number | null | undefined): string {
  if (lots === null || lots === undefined || !Number.isFinite(lots)) return "—";
  return lots.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 3 });
}

/** Decimals for a forex price: the broker's digits when known, else by size (EURUSD 5, USDJPY 3, XAUUSD 2). */
export function fxDigits(price: number, digits?: number | undefined): number {
  if (digits !== undefined && Number.isInteger(digits) && digits >= 0 && digits <= 10) return digits;
  const abs = Math.abs(price);
  return abs >= 1000 ? 2 : abs >= 20 ? 3 : 5;
}

export function formatFxPrice(price: number | null | undefined, digits?: number | undefined): string {
  if (price === null || price === undefined || !Number.isFinite(price)) return "—";
  const d = fxDigits(price, digits);
  return price.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
/** A 10^8-scaled price (int or exact text) as display text. */
export const formatFxPriceE8 = (e8: unknown, digits?: number | undefined): string =>
  formatFxPrice(e8ToUsdt(e8), digits);

/** An R multiple with sign and arrow: "▲ +1.85 R", "▼ −1.02 R". */
export function signedR(r: number | null | undefined): Signed | null {
  if (r === null || r === undefined || !Number.isFinite(r)) return null;
  const rounded = Number(r.toFixed(2));
  if (rounded === 0) return { text: "0.00 R", tone: "flat", arrow: "" };
  const text = `${rounded > 0 ? "+" : "−"}${Math.abs(rounded).toFixed(2)} R`;
  return rounded > 0 ? { text, tone: "gain", arrow: "▲" } : { text, tone: "loss", arrow: "▼" };
}

// ---------------------------------------------------------------- inputs (as stored)

export type FxPositionRaw = {
  symbol: string;
  side: number;
  entry_ms: number;
  stop: number;
  target: number;
  units: string;
  entry_fill: string;
  leverage: number;
  stop_out_price: string;
  margin_e8: number;
  notional_e8: string;
  contract_size: string;
  quote_to_account_entry?: string;
};
export type FxWalletStateRaw = {
  version?: string;
  balance_e8: number;
  used_margin_e8: number;
  seq?: number;
  positions?: Record<string, FxPositionRaw>;
};
/** The published forex quote fields the page needs (`fx_market_quotes`). */
export type FxQuoteLite = { symbol: string; bid: number; ask: number; digits: number; published_at: string };
/** Shared facts of one setup (`fx_market_setups`, plus the final outcome's exit fill). */
export type FxTradeFacts = {
  strategy_id: string;
  side: number | null;
  horizon_min: number | null;
  entry_fill: string | null;
  stop: number | null;
  target: number | null;
  exit_fill: string | null;
};
export type FxConfigFacts = {
  initialBalanceE8: number | null;
  riskFraction: number | null;
  maxExposureMultiple: number | null;
  maxPositions: number | null;
  contractSizes: Record<string, string>;
  version: string | null;
};

/** The parts of the stored `fx-wallet-v1` config the page uses (`FxWalletConfig.to_record`). */
export function fxConfigFacts(config: Record<string, unknown> | null | undefined): FxConfigFacts {
  const c: Record<string, unknown> = config ?? {};
  const contractSizes: Record<string, string> = {};
  const contracts = c["contracts"];
  if (Array.isArray(contracts))
    for (const item of contracts as Record<string, unknown>[])
      if (item && typeof item["symbol"] === "string" && item["contract_size"] !== undefined)
        contractSizes[item["symbol"]] = String(item["contract_size"]);
  const initial = parseExact(c["initial_balance_e8"]);
  const max = parseExact(c["max_positions"]);
  return {
    initialBalanceE8: initial,
    riskFraction: parseExact(c["risk_fraction"]),
    maxExposureMultiple: parseExact(c["max_exposure_multiple"]),
    maxPositions: max,
    contractSizes,
    version: typeof c["version"] === "string" ? c["version"] : null,
  };
}

/** The frozen configuration shown under the results. */
export function fxRunFacts(config: Record<string, unknown> | null | undefined): [string, string][] {
  if (!config) return [];
  const facts = fxConfigFacts(config);
  const out: [string, string][] = [];
  if (facts.version) out.push(["Wallet version", facts.version]);
  if (facts.initialBalanceE8 !== null) out.push(["Starting balance", `${formatUsdt(facts.initialBalanceE8 / E8)} USD`]);
  if (facts.riskFraction !== null) out.push(["Risk per trade", formatPercent(facts.riskFraction, 2)]);
  if (facts.maxExposureMultiple !== null) out.push(["Exposure cap", `${facts.maxExposureMultiple} × balance`]);
  if (facts.maxPositions !== null) out.push(["Max positions", String(facts.maxPositions)]);
  const symbols = Object.keys(facts.contractSizes).sort();
  if (symbols.length) out.push(["Contracts", symbols.join(", ")]);
  return out;
}

// ---------------------------------------------------------------- page state

export type FxPageState = "loading" | "error" | "no_access" | "no_account" | "waiting" | "stale" | "ready";

/**
 * Which screen the forex Paper page shows. Never zeros as if they were results: no account and no run yet are
 * their own states, and an old run is shown with its time instead of looking current.
 */
export function fxPageState(input: {
  access: "loading" | "ready";
  entitled: boolean;
  loaded: boolean;
  error: boolean;
  accountId: string | null;
  boundaryMs: number | null;
  nowMs: number;
}): FxPageState {
  if (input.access === "loading") return "loading";
  if (!input.entitled) return "no_access";
  if (!input.loaded) return input.error ? "error" : "loading";
  if (input.accountId === null) return "no_account";
  if (input.boundaryMs === null) return "waiting";
  return input.nowMs - input.boundaryMs > FX_STALE_AFTER_MS ? "stale" : "ready";
}

// ---------------------------------------------------------------- open positions

export type FxPositionView = {
  id: string;
  symbol: string;
  side: 1 | -1;
  sideLabel: "Long" | "Short";
  lots: number | null;
  lotsText: string;
  entry: string;
  mark: string;
  markStale: boolean;
  hasMark: boolean;
  unrealized: Signed | null;
  unrealizedUsd: number | null;
  margin: string;
  leverage: string;
  stop: string;
  target: string;
  stopOut: string;
  openRiskUsd: number | null;
  openRisk: string;
  strategyId: string | null;
  horizonMin: number | null;
  openedMs: number;
  openedText: string;
  heldText: string;
};

/**
 * One view per open position. Unrealized P&L closes a long at the published bid and a short at the ask, converted
 * to USD at the position's entry conversion (exact for USD-quoted symbols, an approximation for USDJPY). Open risk
 * is the loss at the stop from the entry fill, at the same conversion.
 */
export function fxPositionViews(
  state: FxWalletStateRaw | null,
  quotes: readonly FxQuoteLite[],
  nowMs: number,
  meta: Readonly<Record<string, { strategy_id: string; horizon_min: number | null }>> = {},
): FxPositionView[] {
  if (!state) return [];
  const bySymbol = new Map(quotes.map((quote) => [quote.symbol, quote]));
  const views: FxPositionView[] = [];
  for (const [id, p] of Object.entries(state.positions ?? {})) {
    const side: 1 | -1 = p.side === -1 ? -1 : 1;
    const units = parseExact(p.units);
    const entry = e8ToUsdt(p.entry_fill);
    const stop = e8ToUsdt(p.stop);
    const conv = parseExact(p.quote_to_account_entry ?? null);
    const quote = bySymbol.get(p.symbol);
    const digits = quote?.digits;
    const price = quote ? (side === 1 ? quote.bid : quote.ask) : null;
    const unrealizedUsd =
      price !== null && units !== null && entry !== null && conv !== null ? side * units * (price - entry) * conv : null;
    const openRiskUsd =
      units !== null && entry !== null && stop !== null && conv !== null ? units * Math.abs(entry - stop) * conv : null;
    const lots = lotsOf(p.units, p.contract_size);
    views.push({
      id,
      symbol: p.symbol,
      side,
      sideLabel: side === 1 ? "Long" : "Short",
      lots,
      lotsText: formatLots(lots),
      entry: formatFxPrice(entry, digits),
      mark: formatFxPrice(price, digits),
      markStale: quote ? nowMs - Date.parse(quote.published_at) > FX_QUOTE_STALE_MS : false,
      hasMark: price !== null,
      unrealized: signedUsdt(unrealizedUsd),
      unrealizedUsd,
      margin: formatUsdt(e8ToUsdt(p.margin_e8)),
      leverage: `${p.leverage}×`,
      stop: formatFxPrice(stop, digits),
      target: formatFxPriceE8(p.target, digits),
      stopOut: formatFxPriceE8(p.stop_out_price, digits),
      openRiskUsd,
      openRisk: formatUsdt(openRiskUsd),
      strategyId: meta[id]?.strategy_id ?? null,
      horizonMin: meta[id]?.horizon_min ?? null,
      openedMs: p.entry_ms,
      openedText: dateTimeText(p.entry_ms),
      heldText: formatDuration(nowMs - p.entry_ms),
    });
  }
  return views.sort((a, b) => b.openedMs - a.openedMs || (a.id < b.id ? -1 : 1));
}

// ---------------------------------------------------------------- account headline

export type FxAccountView = {
  balanceUsd: number | null;
  equityUsd: number | null;
  unrealized: Signed | null;
  unmarked: number;
  usedMarginUsd: number | null;
  freeMarginUsd: number | null;
  utilization: number | null;
  openPositions: number;
  openRiskUsd: number | null;
  openRiskFraction: number | null;
  today: Signed | null;
  total: Signed | null;
  totalFraction: number | null;
};

/**
 * The headline. Total P&L = balance − starting balance (realized, after costs); today = balance − the balance of
 * the last line before 00:00 UTC (the starting balance when there is none). Equity adds the unrealized P&L of the
 * positions that have a price; the others are counted in `unmarked`.
 */
export function fxAccountView(
  state: FxWalletStateRaw | null,
  positions: readonly FxPositionView[],
  facts: { initialBalanceE8: number | null; dayStartBalanceE8: number | null },
): FxAccountView {
  const balance = state ? e8ToUsdt(state.balance_e8) : null;
  const used = state ? e8ToUsdt(state.used_margin_e8) : null;
  const marked = positions.filter((p) => p.unrealizedUsd !== null);
  const unrealizedUsd = marked.reduce((sum, p) => sum + (p.unrealizedUsd ?? 0), 0);
  const risks = positions.map((p) => p.openRiskUsd);
  const openRiskUsd = risks.every((r) => r !== null) ? risks.reduce((s: number, r) => s + (r ?? 0), 0) : null;
  const initial = facts.initialBalanceE8 === null ? null : facts.initialBalanceE8 / E8;
  const dayStart = facts.dayStartBalanceE8 !== null ? facts.dayStartBalanceE8 / E8 : initial;
  return {
    balanceUsd: balance,
    equityUsd: balance === null ? null : balance + unrealizedUsd,
    unrealized: positions.length === 0 ? signedUsdt(0) : marked.length ? signedUsdt(unrealizedUsd) : null,
    unmarked: positions.length - marked.length,
    usedMarginUsd: used,
    freeMarginUsd: balance === null || used === null ? null : balance - used,
    utilization: balance !== null && balance > 0 && used !== null ? used / balance : null,
    openPositions: positions.length,
    openRiskUsd,
    openRiskFraction: openRiskUsd !== null && balance !== null && balance > 0 ? openRiskUsd / balance : null,
    today: balance !== null && dayStart !== null ? signedUsdt(balance - dayStart) : null,
    total: balance !== null && initial !== null ? signedUsdt(balance - initial) : null,
    totalFraction: balance !== null && initial !== null && initial > 0 ? (balance - initial) / initial : null,
  };
}

/** 00:00 UTC of the day `nowMs` is in. */
export const utcDayStart = (nowMs: number): number => nowMs - (((nowMs % 86_400_000) + 86_400_000) % 86_400_000);

// ---------------------------------------------------------------- closed trades

export type FxClosedTradeView = {
  id: string;
  symbol: string;
  side: 1 | -1 | null;
  strategyId: string | null;
  horizonMin: number | null;
  openedMs: number;
  closedMs: number;
  closedText: string;
  heldText: string;
  lots: number | null;
  lotsText: string;
  entry: string;
  exit: string;
  gross: Signed | null;
  costs: Signed | null;
  swap: Signed | null;
  net: Signed | null;
  netE8: number;
  netR: Signed | null;
  reason: FxCloseReason;
};

/**
 * Closed trades from complete per-setup ledger lines (a stop-out or a result closes a forex trade; there are no
 * partial exits). Net = every money line; R = net ÷ the risk budget at entry (risk fraction × the balance just
 * before the opening commission), so a full stop is about −1 R plus costs. The exit is the outcome's exit fill;
 * a stop-out has none stored, so it shows "—".
 */
export function fxClosedTradeViews(
  lines: readonly LedgerRow[],
  facts: Readonly<Record<string, FxTradeFacts>>,
  config: Pick<FxConfigFacts, "riskFraction" | "contractSizes">,
  digits: Readonly<Record<string, number>> = {},
): FxClosedTradeView[] {
  const bySetup = new Map<string, LedgerRow[]>();
  for (const line of lines) {
    if (!line.setup_id || !MONEY.has(line.type)) continue;
    const list = bySetup.get(line.setup_id) ?? [];
    list.push(line);
    bySetup.set(line.setup_id, list);
  }
  const trades: FxClosedTradeView[] = [];
  for (const [setupId, setupLines] of bySetup) {
    const closing = setupLines
      .filter((line) => line.type === "liquidation" || line.type === "pnl")
      .sort((a, b) => b.seq - a.seq)[0];
    if (!closing) continue;
    const sum = (kinds: string[]) =>
      setupLines.filter((line) => kinds.includes(line.type)).reduce((s, line) => s + line.amount_e8, 0);
    const open = setupLines.find((line) => line.type === "open_fee");
    const openedMs = open ? Math.min(open.ms, closing.ms) : closing.ms;
    const net = sum([...MONEY]);
    const budget =
      open && config.riskFraction !== null ? (open.balance_e8 - open.amount_e8) * config.riskFraction : null;
    const fact = facts[setupId];
    const symbol = closing.symbol ?? "";
    const lots = lotsOf(open?.payload?.["units"], config.contractSizes[symbol]);
    const reason = fxCloseReason(closing);
    const d = digits[symbol];
    trades.push({
      id: setupId,
      symbol,
      side: fact?.side === 1 ? 1 : fact?.side === -1 ? -1 : null,
      strategyId: fact?.strategy_id ?? null,
      horizonMin: fact?.horizon_min ?? null,
      openedMs,
      closedMs: closing.ms,
      closedText: dateTimeText(closing.ms),
      heldText: formatDuration(Math.max(0, closing.ms - openedMs)),
      lots,
      lotsText: formatLots(lots),
      entry: formatFxPriceE8(fact?.entry_fill ?? null, d),
      exit: reason === "stop_out" ? "—" : formatFxPriceE8(fact?.exit_fill ?? null, d),
      gross: signedE8(sum(["pnl", "liquidation"])),
      costs: signedE8(sum(["open_fee", "close_fee"])),
      swap: signedE8(sum(["funding"])),
      net: signedE8(net),
      netE8: net,
      netR: budget !== null && budget > 0 ? signedR(net / budget) : null,
      reason,
    });
  }
  return trades.sort((a, b) => b.closedMs - a.closedMs || (a.id < b.id ? -1 : 1));
}

// ---------------------------------------------------------------- rejected

export type FxRejectedView = {
  seq: number;
  ms: number;
  timeText: string;
  symbol: string;
  setupId: string;
  strategyId: string | null;
  side: 1 | -1 | null;
  code: string;
  label: string;
  help: string;
  icon: FxIconKey;
};

export function fxRejectedViews(
  lines: readonly LedgerRow[],
  facts: Readonly<Record<string, Pick<FxTradeFacts, "strategy_id" | "side">>> = {},
): FxRejectedView[] {
  return lines
    .filter((line) => line.type === "rejected")
    .map((line): FxRejectedView => {
      const reason = line.payload?.["reason"];
      const entry = fxRejection(typeof reason === "string" ? reason : "");
      const fact = line.setup_id ? facts[line.setup_id] : undefined;
      return {
        seq: line.seq,
        ms: line.ms,
        timeText: dateTimeText(line.ms),
        symbol: line.symbol ?? "",
        setupId: line.setup_id ?? "",
        strategyId: fact?.strategy_id ?? null,
        side: fact?.side === 1 ? 1 : fact?.side === -1 ? -1 : null,
        code: entry.code,
        label: entry.label,
        help: entry.help,
        icon: entry.icon,
      };
    })
    .sort((a, b) => b.seq - a.seq);
}

// ---------------------------------------------------------------- ledger

export type FxLedgerView = {
  seq: number;
  ms: number;
  timeText: string;
  type: string;
  label: string;
  icon: FxIconKey;
  symbol: string;
  amount: Signed | null;
  balance: string;
  detail: string;
};

/** One row per line; a rejection moves no money and shows no amount, not a zero. */
export function fxLedgerViews(lines: readonly LedgerRow[]): FxLedgerView[] {
  return lines.map((line): FxLedgerView => {
    const payload = line.payload ?? {};
    const kind = fxLedgerType(line.type);
    let detail = "";
    if (line.type === "rejected") detail = fxRejection(String(payload["reason"] ?? "")).label;
    else if (line.type === "pnl") detail = FX_CLOSE_REASONS[fxCloseReason(line)].label;
    else if (line.type === "funding" && Number(payload["multiplier"]) > 1) detail = `× ${Number(payload["multiplier"])} rollover`;
    else if (line.type === "open_fee" && payload["leverage"] !== undefined) detail = `${String(payload["leverage"])}× leverage`;
    return {
      seq: line.seq,
      ms: line.ms,
      timeText: dateTimeText(line.ms),
      type: line.type,
      label: kind.label,
      icon: kind.icon,
      symbol: line.symbol ?? "",
      amount: MONEY.has(line.type) ? signedE8(line.amount_e8) : null,
      balance: formatUsdt(e8ToUsdt(line.balance_e8)),
      detail,
    };
  });
}

/** Tone of a close reason for the chip colour (with its icon and word, never colour alone). */
export const reasonTone = (reason: FxCloseReason): Tone =>
  FX_CLOSE_REASONS[reason].tone === "good" ? "gain" : FX_CLOSE_REASONS[reason].tone === "bad" ? "loss" : "flat";
