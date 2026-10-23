import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";

import { assertMarketEnabled, requireMarketAccess } from "@/integrations/supabase/auth-middleware";
import type { JsonObject } from "./paper/paper-account";
import {
  loadFxPaperAccount,
  loadPaperAccount,
  loadPaperLedger,
  loadPaperRejected,
  loadPaperTrades,
} from "./paper/paper-data";
import { accountMarketOf } from "./paper/paper-view";

/**
 * Paper account page (#36 Part 3; one account per market since #454). Every read uses the caller's own client, so
 * RLS limits it to their rows; the Worker only forwards rows and the browser maps them (nothing heavy runs here).
 * Every input names its market, and a list read whose account id belongs to the other market is refused, so the
 * two accounts are never mixed.
 */
const market = z.enum(["crypto", "forex"]).default("crypto");
const accountInput = z.object({ market }).default({ market: "crypto" });
const pageInput = z
  .object({
    market,
    accountId: z.string().min(1).max(64),
    cursor: z
      .string()
      .regex(/^\d{1,18}$/)
      .nullable()
      .default(null),
  })
  .refine((input) => accountMarketOf(input.accountId) === input.market, {
    message: "Account does not belong to this market",
  });

// The rows are stored JSON; the loaders' own types are richer than the server-function wire check accepts, and the
// page casts the response back to them (`PaperAccountData`, `Page`), so the wire type is plain JSON.
const wire = (value: unknown) => value as JsonObject;

export const getPaperAccount = createServerFn({ method: "GET" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => accountInput.parse(input ?? undefined))
  .handler(async ({ context, data }) => {
    assertMarketEnabled(context.markets, data.market);
    return wire(
      data.market === "forex"
        ? await loadFxPaperAccount(context.supabase as never, Date.now())
        : await loadPaperAccount(context.supabase as never),
    );
  });

export const getPaperTrades = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) => {
    assertMarketEnabled(context.markets, data.market);
    return wire(
      await loadPaperTrades(context.supabase as never, data.accountId, data.cursor, undefined, data.market),
    );
  });

export const getPaperRejected = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) => {
    assertMarketEnabled(context.markets, data.market);
    return wire(
      await loadPaperRejected(context.supabase as never, data.accountId, data.cursor, undefined, data.market),
    );
  });

export const getPaperLedger = createServerFn({ method: "POST" })
  .middleware([requireMarketAccess])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) => {
    assertMarketEnabled(context.markets, data.market);
    return wire(
      await loadPaperLedger(context.supabase as never, data.accountId, data.cursor, undefined, data.market),
    );
  });
