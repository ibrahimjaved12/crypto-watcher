/** Client-side data access. RLS keeps every row scoped to the signed-in user. */
import { supabase } from "@/integrations/supabase/client";
import { DEFAULT_SYMBOLS, instrumentId, MAX_WATCHLIST_SIZE } from "@/lib/market/symbols";

export type WatchlistItem = {
  id: string;
  instrument_id: string;
  symbol: string;
  created_at: string;
};
export type Settings = {
  user_id: string;
  threshold_pct: number;
  window_minutes: number;
  cooldown_minutes: number;
  monitoring_enabled: boolean;
  market_data_collection_enabled: boolean;
  completed_candle_ta_enabled: boolean;
  movement_alerts_enabled: boolean;
  developing_setup_evaluation_enabled: boolean;
  paper_trading_enabled: boolean;
};
export type AlertRow = {
  id: string;
  symbol: string;
  triggered_at: string;
  change_pct: number;
  window_minutes: number | null;
  comparison_mode: string;
  baseline_price: number | null;
  baseline_at: string | null;
  observed_at: string | null;
  threshold_pct: number;
  rule: string;
  price: number | null;
  data_source: string;
  is_test: boolean;
};
export type NoteRow = {
  id: string;
  symbol: string | null;
  title: string;
  body: string;
  created_at: string;
  updated_at: string;
};
export type MonitorRun = {
  id: string;
  ran_at: string;
  status: string;
  symbols_checked: number;
  alerts_created: number;
  data_source: string | null;
  error_message: string | null;
};

async function userId(): Promise<string> {
  const { data } = await supabase.auth.getUser();
  if (!data.user) throw new Error("Not signed in");
  return data.user.id;
}

/** Returns the watchlist, seeding the three starter pairs on first use. */
export async function fetchWatchlist(): Promise<WatchlistItem[]> {
  const uid = await userId();
  const { data, error } = await supabase
    .from("watchlist_items")
    .select("id, instrument_id, symbol, created_at")
    .order("created_at", { ascending: true });
  if (error) throw error;
  if (data && data.length > 0) return data as WatchlistItem[];

  await supabase.from("watchlist_items").insert(
    DEFAULT_SYMBOLS.map((symbol) => ({
      user_id: uid,
      symbol,
      instrument_id: instrumentId(symbol),
    })),
  );
  const seeded = await supabase
    .from("watchlist_items")
    .select("id, instrument_id, symbol, created_at")
    .order("created_at", { ascending: true });
  if (seeded.error) throw seeded.error;
  return (seeded.data ?? []) as WatchlistItem[];
}

export async function addSymbol(symbol: string): Promise<void> {
  const uid = await userId();
  const { count } = await supabase
    .from("watchlist_items")
    .select("id", { count: "exact", head: true });
  if ((count ?? 0) >= MAX_WATCHLIST_SIZE) {
    throw new Error(`You can follow at most ${MAX_WATCHLIST_SIZE} pairs.`);
  }
  const { error } = await supabase
    .from("watchlist_items")
    .insert({ user_id: uid, symbol, instrument_id: instrumentId(symbol) });
  if (error) throw error;
}

export async function removeSymbol(id: string): Promise<void> {
  const { error } = await supabase.from("watchlist_items").delete().eq("id", id);
  if (error) throw error;
}

export async function fetchSettings(): Promise<Settings> {
  const uid = await userId();
  const { data, error } = await supabase
    .from("monitor_settings")
    .select("*")
    .eq("user_id", uid)
    .maybeSingle();
  if (error) throw error;
  if (data) return data as unknown as Settings;
  const created = await supabase
    .from("monitor_settings")
    .insert({ user_id: uid })
    .select("*")
    .single();
  if (created.error) throw created.error;
  return created.data as unknown as Settings;
}

export async function saveSettings(patch: Partial<Settings>): Promise<void> {
  const uid = await userId();
  const { error } = await supabase.from("monitor_settings").update(patch).eq("user_id", uid);
  if (error) throw error;
}

export async function fetchAlerts(): Promise<AlertRow[]> {
  const { data, error } = await supabase
    .from("alerts")
    .select("*")
    .order("triggered_at", { ascending: false })
    .limit(500);
  if (error) throw error;
  return (data ?? []) as unknown as AlertRow[];
}

export async function createTestAlert(symbol: string): Promise<void> {
  const uid = await userId();
  const { error } = await supabase.from("alerts").insert({
    user_id: uid,
    symbol,
    instrument_id: instrumentId(symbol),
    change_pct: 0,
    window_minutes: 15,
    threshold_pct: 0,
    rule: "Manual test alert (not a market event)",
    price: null,
    data_source: "test",
    price_type: "not_applicable",
    is_test: true,
  });
  if (error) throw error;
}

export async function deleteAlert(id: string): Promise<void> {
  const { error } = await supabase.from("alerts").delete().eq("id", id);
  if (error) throw error;
}

export async function fetchNotes(): Promise<NoteRow[]> {
  const { data, error } = await supabase
    .from("notes")
    .select("*")
    .order("updated_at", { ascending: false });
  if (error) throw error;
  return (data ?? []) as unknown as NoteRow[];
}

export async function createNote(input: {
  title: string;
  body: string;
  symbol: string | null;
}): Promise<void> {
  const uid = await userId();
  const { error } = await supabase.from("notes").insert({
    user_id: uid,
    ...input,
    instrument_id: input.symbol ? instrumentId(input.symbol) : null,
  });
  if (error) throw error;
}

export async function deleteNote(id: string): Promise<void> {
  const { error } = await supabase.from("notes").delete().eq("id", id);
  if (error) throw error;
}

export async function fetchRuns(): Promise<MonitorRun[]> {
  const { data, error } = await supabase
    .from("monitor_runs")
    .select("*")
    .order("ran_at", { ascending: false })
    .limit(25);
  if (error) throw error;
  return (data ?? []) as unknown as MonitorRun[];
}
