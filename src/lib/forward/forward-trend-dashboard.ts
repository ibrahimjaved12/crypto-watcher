import type { SupabaseClient } from "@supabase/supabase-js";
import type { SampleKind, TrendSample, TrendState } from "./forward-trend-contract";
import { readTrendPages } from "./forward-trend-pages";

export type TrendLedgerPoint = {
  track: string;
  day_ms: number;
  sample_kind: SampleKind;
  sample_equity_ppm: number;
};
export type TrackInfo = { name: string; kind: string; control: string | null };

/** Format Python's canonical sample statistics. No power/statistical calculations live in the UI. */
export function summarizeTrack(
  info: TrackInfo,
  rows: TrendLedgerPoint[],
  state: TrendState | undefined,
  sampleKind: SampleKind = "prospective",
) {
  const sample: TrendSample | undefined = state?.samples[sampleKind];
  const n = sample?.n_days ?? 0;
  const muMin = sample?.mu_min_daily ?? null;
  const mean = sample?.mean_daily ?? null;
  const needed = sample?.days_needed ?? null;
  const pct = (value: number) => `${(value * 100).toFixed(3)}%`;
  const edgeLine =
    muMin === null
      ? `${n} days: not enough observations to estimate a minimum detectable edge`
      : `Minimum detectable edge ${pct(muMin)}/day (normal approximation, independent daily returns, two-sided α=0.05, 80% power)` +
        (needed !== null
          ? `; about ${needed} days at the observed mean and volatility`
          : "; observed mean is not positive");
  return {
    track: info.name,
    kind: info.kind,
    control: info.control,
    days: n,
    prospectiveDays: state?.samples.prospective.n_days ?? 0,
    retrospectiveDays: state?.samples.retrospective.n_days ?? 0,
    sampleKind,
    meanDaily: mean,
    sdDaily: sample?.sd_daily ?? null,
    muMinDaily: muMin,
    daysNeeded: needed,
    edgeLine,
    meanTurnover: sample?.mean_turnover ?? null,
    meanGross: sample?.mean_gross ?? null,
    equity: rows
      .filter((row) => row.track === info.name && row.sample_kind === sampleKind)
      .sort((a, b) => a.day_ms - b.day_ms)
      .map((row) => ({ day_ms: row.day_ms, equity: row.sample_equity_ppm / 1e6 })),
  };
}

type LatestTrendRun = {
  run_key: string;
  status: string;
  reason: string | null;
  through_day_ms: number | null;
  tracks: TrackInfo[];
  states: Record<string, TrendState>;
  created_at: string;
};

export async function loadTrendDashboard(client: SupabaseClient) {
  // The account transaction lock orders writes. Its final state timestamp bounds immutable rows
  // before pagination, so overlapping commits cannot shift the captured sample.
  const state = await client
    .from("forward_trend_state")
    .select("revision, run_key, status, reason, through_day_ms, tracks, states, created_at")
    .eq("committed", true)
    .order("revision", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (state.error) throw new Error(`Forward trend dashboard read failed: ${state.error.message}`);
  const latest = state.data as (LatestTrendRun & { revision: number }) | null;
  const diagnostics = await client
    .from("forward_trend_state")
    .select("status, reason, created_at")
    .order("revision", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (diagnostics.error)
    throw new Error(`Forward trend dashboard read failed: ${diagnostics.error.message}`);
  if (!latest)
    return {
      latestRun: null,
      latestAttempt: diagnostics.data,
      tracks: [],
      reconstructedTracks: [],
    };
  const [ledger, weights] = await Promise.all([
    readTrendPages<TrendLedgerPoint & { run_key: string }>((offset) =>
      client
        .from("forward_trend_ledger")
        .select("track, day_ms, sample_kind, sample_equity_ppm, run_key")
        .lte("created_at", latest.created_at)
        .lte("day_ms", latest.through_day_ms ?? 0)
        .order("day_ms")
        .order("track")
        .range(offset, offset + 499),
    ),
    readTrendPages<{
      track: string;
      day_ms: number;
      weights: Record<string, number>;
      decided_at_ms: number;
      recorded_at_ms: number;
      sample_kind: SampleKind;
      run_key: string;
    }>((offset) =>
      client
        .from("forward_trend_weights")
        .select("track, day_ms, weights, decided_at_ms, recorded_at_ms, sample_kind, run_key")
        .lte("created_at", latest.created_at)
        .order("day_ms", { ascending: false })
        .order("track")
        .range(offset, offset + 499),
    ),
  ]);
  const current = new Map<string, (typeof weights)[number]>();
  for (const row of weights) if (!current.has(row.track)) current.set(row.track, row);
  const rows = ledger;
  const summaries = (sampleKind: SampleKind) =>
    latest.tracks.map((info) => ({
      ...summarizeTrack(info, rows, latest.states[info.name], sampleKind),
      currentWeights: current.get(info.name) ?? null,
    }));
  return {
    latestRun: latest,
    latestAttempt: diagnostics.data,
    tracks: summaries("prospective"),
    reconstructedTracks: summaries("retrospective"),
  };
}
