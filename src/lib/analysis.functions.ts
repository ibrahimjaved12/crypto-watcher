import { createServerFn } from "@tanstack/react-start";
import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";
import { analysisInput } from "./analysis.contract";

export const runPythonAnalysis = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => analysisInput.parse(input))
  .handler(async ({ context, data }) => {
    const { analyzeForUser } = await import("./analysis.server");
    return analyzeForUser(context.supabase, context.userId, data.symbol);
  });
