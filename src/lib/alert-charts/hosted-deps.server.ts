/** Real dependencies of the hosted chart functions: the service-role client and the private bucket. */
import { supabaseAdmin } from "@/integrations/supabase/client.server";
import { CHART_BUCKET, hostedCaps, SIGNED_URL_SECONDS } from "./hosted";
import type { DeleteDeps, HostedDeps, ImageDeps } from "./hosted.server";

type Admin = typeof supabaseAdmin;
const bucket = (admin: Admin) => admin.storage.from(CHART_BUCKET);

async function chartRow(admin: Admin, alertId: string) {
  const { data, error } = await admin.from("alert_chart_files")
    .select("user_id, status, storage_path").eq("alert_id", alertId).maybeSingle();
  if (error) throw new Error("Chart lookup failed");
  return data;
}

export function hookDeps(admin: Admin = supabaseAdmin): HostedDeps {
  return {
    rpc: async (name, args) => {
      const { data, error } = await (admin.rpc as unknown as (n: string, a: Record<string, unknown>) =>
        Promise<{ data: unknown; error: { message: string } | null }>)(name, args);
      return { data, error };
    },
    signedUploadUrl: async (path) => {
      const { data, error } = await bucket(admin).createSignedUploadUrl(path, { upsert: true });
      return error || !data ? null : { signedUrl: data.signedUrl, token: data.token };
    },
    objectSize: async (path) => {
      const slash = path.lastIndexOf("/");
      const { data, error } = await bucket(admin).list(path.slice(0, slash), { limit: 1, search: path.slice(slash + 1) });
      if (error) throw new Error("Storage lookup failed");
      const size = data?.find((file) => file.name === path.slice(slash + 1))?.metadata?.["size"];
      return typeof size === "number" ? size : null;
    },
    publishableKey: process.env["SUPABASE_PUBLISHABLE_KEY"] ?? "",
    caps: hostedCaps(),
  };
}

export function imageDeps(admin: Admin = supabaseAdmin): ImageDeps {
  return {
    chartRow: (alertId) => chartRow(admin, alertId),
    signedUrls: async (path, filename) => {
      const [view, download] = await Promise.all([
        bucket(admin).createSignedUrl(path, SIGNED_URL_SECONDS),
        bucket(admin).createSignedUrl(path, SIGNED_URL_SECONDS, { download: filename }),
      ]);
      return view.error || download.error || !view.data || !download.data
        ? null : { view: view.data.signedUrl, download: download.data.signedUrl };
    },
  };
}

export function deleteDeps(userClient: { from: Admin["from"] }, admin: Admin = supabaseAdmin): DeleteDeps {
  return {
    chartRow: (alertId) => chartRow(admin, alertId) as Promise<{ user_id: string; storage_path: string | null } | null>,
    removeObject: async (path) => {
      const { error } = await bucket(admin).remove([path]);
      if (error) throw new Error("The chart file could not be removed; the alert was kept. Try again.");
    },
    deleteAlert: async (alertId) => {
      const { data, error } = await userClient.from("alerts").delete().eq("id", alertId).select("id");
      if (error) throw new Error(error.message);
      return data?.length ?? 0;
    },
  };
}
