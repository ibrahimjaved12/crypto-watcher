import type { SupabaseClient } from "@supabase/supabase-js";

/** Default daily scheduling covers every registered account. An optional UUID narrows a manual run. */
export async function listForwardUsers(client: SupabaseClient, userId?: string): Promise<string[]> {
  if (userId) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(userId)) {
      throw new Error("FORWARD_USER_ID must be a valid account UUID when provided");
    }
    return [userId];
  }
  const users = new Set<string>();
  for (let page = 1; ; page++) {
    const { data, error } = await client.auth.admin.listUsers({ page, perPage: 500 });
    if (error) throw new Error(`Forward account listing failed: ${error.message}`);
    if (!data.users.length) return [...users];
    for (const user of data.users) users.add(user.id);
  }
}
