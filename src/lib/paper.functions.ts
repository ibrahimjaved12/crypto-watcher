import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import type { JsonObject } from "./paper/paper-account";
import {
  loadPaperAccount,
  loadPaperLedger,
  loadPaperRejected,
  loadPaperTrades,
} from "./paper/paper-data";

/**
 * Paper account page (#36 Part 3). Every read uses the caller's own client, so RLS limits it to their rows; the
 * Worker only forwards rows and the browser maps them (nothing heavy runs here).
 */
const pageInput = z.object({
  accountId: z.string().min(1).max(64),
  cursor: z
    .string()
    .regex(/^\d{1,18}$/)
    .nullable()
    .default(null),
});

// The rows are stored JSON; the loaders' own types are richer than the server-function wire check accepts, and the
// page casts the response back to them (`PaperAccountData`, `Page`), so the wire type is plain JSON.
const wire = (value: unknown) => value as JsonObject;

export const getPaperAccount = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .handler(async ({ context }) => wire(await loadPaperAccount(context.supabase as never)));

export const getPaperTrades = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) =>
    wire(await loadPaperTrades(context.supabase as never, data.accountId, data.cursor)),
  );

export const getPaperRejected = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) =>
    wire(await loadPaperRejected(context.supabase as never, data.accountId, data.cursor)),
  );

export const getPaperLedger = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => pageInput.parse(input))
  .handler(async ({ context, data }) =>
    wire(await loadPaperLedger(context.supabase as never, data.accountId, data.cursor)),
  );
