import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { z } from "zod";
import { analysisInput, analysisResponse, type AnalysisReply } from "./analysis.contract";

const runnerAnalysisReply = z.discriminatedUnion("ok", [
  z
    .object({
      ok: z.literal(true),
      analysis: analysisResponse,
      metrics: z
        .object({
          duration_ms: z.number().finite().nonnegative(),
          request_bytes: z.number().int().nonnegative(),
          response_bytes: z.number().int().nonnegative(),
        })
        .strict(),
    })
    .strict(),
  z
    .object({
      ok: z.literal(false),
      error: z.string().min(1).max(1_000),
      category: z.string().min(1).max(64),
    })
    .strict(),
]);

export const runPythonAnalysis = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => analysisInput.parse(input))
  .handler(async ({ context, data }): Promise<AnalysisReply> => {
    const { runnerConfig, callRunner, RunnerConfigurationError } = await import(
      "./runner/client.server"
    );
    let config;
    try {
      config = runnerConfig();
    } catch (error) {
      if (error instanceof RunnerConfigurationError) {
        return { ok: false, error: error.message, category: error.category };
      }
      throw error;
    }
    if (config !== null) {
      const reply = await callRunner("/v1/analysis", context.accessToken, { symbol: data.symbol });
      if (!reply.ok) return reply;
      const parsed = runnerAnalysisReply.safeParse(reply.data);
      if (
        !parsed.success ||
        (parsed.data.ok &&
          (parsed.data.analysis.symbol !== data.symbol ||
            parsed.data.analysis.instrument.native_symbol !== data.symbol))
      ) {
        return {
          ok: false,
          category: "invalid_response",
          error: "The runner returned an invalid analysis response. Try again.",
        };
      }
      return parsed.data;
    }
    const { analyzeForUser } = await import("./analysis.server");
    return analyzeForUser(context.supabase, context.userId, data.symbol);
  });
