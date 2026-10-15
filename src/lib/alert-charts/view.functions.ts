import { createServerFn } from "@tanstack/react-start";
import { z } from "zod";

import { requireSupabaseAuth } from "@/integrations/supabase/auth-middleware";

const alertInput = z.object({ alertId: z.string().uuid() });

/**
 * A short-lived signed URL for the chart of one of the caller's alerts. Another user's alert id is refused with
 * the same "Chart not found" as an unknown id. The Worker only signs a URL; the browser fetches the image
 * from storage, so no image is fetched, resized or rendered here.
 */
export const getAlertChartImage = createServerFn({ method: "GET" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => alertInput.parse(input))
  .handler(async ({ context, data }) => {
    const { chartImageFor } = await import("./hosted.server");
    const { imageDeps } = await import("./hosted-deps.server");
    return chartImageFor(imageDeps(), context.userId, data.alertId);
  });

/** Deletes an alert together with its hosted chart (storage object and metadata row). */
export const deleteAlertWithChart = createServerFn({ method: "POST" })
  .middleware([requireSupabaseAuth])
  .validator((input: unknown) => alertInput.parse(input))
  .handler(async ({ context, data }) => {
    const { deleteAlertAndChart } = await import("./hosted.server");
    const { deleteDeps } = await import("./hosted-deps.server");
    return deleteAlertAndChart(deleteDeps(context.supabase), context.userId, data.alertId);
  });
