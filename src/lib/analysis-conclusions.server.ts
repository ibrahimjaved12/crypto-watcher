import type { SupabaseClient } from "@supabase/supabase-js";
import { supabaseAdmin } from "@/integrations/supabase/client.server";
import type { Database } from "@/integrations/supabase/types";

type Client = Pick<SupabaseClient<Database>, "from">;
export type AnalysisConclusion = Database["public"]["Tables"]["analysis_conclusions"]["Row"];
export type AnalysisConclusionInsert =
  Database["public"]["Tables"]["analysis_conclusions"]["Insert"];

/** Server-only append path. The default service client is the only role granted INSERT. */
export async function persistAnalysisConclusion(
  input: AnalysisConclusionInsert,
  db: Client = supabaseAdmin,
): Promise<AnalysisConclusion> {
  const inserted = await db.from("analysis_conclusions").insert(input).select("*").maybeSingle();
  if (!inserted.error && inserted.data) return inserted.data;
  if (inserted.error?.code !== "23505") {
    throw new Error("Could not persist analysis conclusion");
  }

  const existing = await db
    .from("analysis_conclusions")
    .select("*")
    .eq("user_id", input.user_id)
    .eq("idempotency_key", input.idempotency_key)
    .maybeSingle();
  if (existing.error || !existing.data) {
    throw new Error("Could not resolve idempotent analysis conclusion");
  }
  if (
    existing.data.input_hash !== (input.input_hash ?? null) ||
    existing.data.input_reference !== (input.input_reference ?? null)
  ) {
    throw new Error("Analysis conclusion idempotency key was reused for different input");
  }
  return existing.data;
}

/** Explicit user predicate also protects account scope when a service client reads. */
export async function listAnalysisConclusionsForUser(
  userId: string,
  limit = 100,
  db: Client = supabaseAdmin,
): Promise<AnalysisConclusion[]> {
  if (!userId) throw new Error("User is required");
  const boundedLimit = Math.max(1, Math.min(200, Math.trunc(limit)));
  const result = await db
    .from("analysis_conclusions")
    .select("*")
    .eq("user_id", userId)
    .order("evaluated_at", { ascending: false })
    .limit(boundedLimit);
  if (result.error) throw new Error("Could not read analysis conclusions");
  return result.data ?? [];
}
