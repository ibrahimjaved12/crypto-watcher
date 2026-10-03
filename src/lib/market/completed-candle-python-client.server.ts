import { z } from "zod";
import { pythonServiceConfig } from "../python-service.server";
import { completedMarketCandles, missingCompletedCandleOpenTimes, validateCompletedCandleSeries,
  type CompletedCandleSeries } from "./completed-candle-contract";

const timestamp = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const count = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const summary = z.object({
  schema_version: z.literal(1), contract_version: z.literal("completed-candle-v1"),
  provider: z.literal("binance-usdm"), exchange: z.literal("binance"),
  instrument_id: z.string(), price_type: z.literal("trade"), timeframe_minutes: z.literal(1),
  observation_count: count, candle_count: count,
  first_open_time_ms: timestamp.nullable(), last_open_time_ms: timestamp.nullable(),
  missing_open_times_ms: z.array(timestamp),
}).strict();

export async function validatePythonCompletedCandleSeries(
  series: CompletedCandleSeries,
  env: Record<string, string | undefined> = process.env,
  send: typeof fetch = fetch,
) {
  validateCompletedCandleSeries(series);
  const config = pythonServiceConfig("/v1/completed-candles/validate", env);
  const id = series.identity;
  const request = {
    contract_version: series.contractVersion,
    identity: { provider: id.provider, exchange: id.exchange, market_type: id.marketType,
      contract_type: id.contractType, instrument_id: id.instrumentId, symbol: id.symbol,
      native_symbol: id.nativeSymbol, price_type: id.priceType, series_basis: id.seriesBasis,
      timeframe_minutes: id.timeframeMinutes },
    observations: series.observations.map(({ candle: c, provenance: p }) => ({
      candle: { open_time_ms: c.openTime, close_time_ms: c.closeTime,
        open: String(c.open), high: String(c.high), low: String(c.low), close: String(c.close),
        base_volume: String(c.baseVolume), quote_volume: String(c.quoteVolume) },
      provenance: p.sourceKind === "websocket"
        ? { source_kind: "websocket", endpoint: p.endpoint,
            source_event_time_ms: p.sourceEventTime, received_at_ms: p.receivedAt }
        : p.sourceKind === "rest"
          ? { source_kind: "rest", endpoint: p.endpoint, retrieved_at_ms: p.retrievedAt }
          : { source_kind: "archive", dataset_id: p.datasetId, dataset_version: p.datasetVersion,
              dataset_content_sha256: p.datasetContentSha256 },
    })),
  };
  const response = await send(config.url, { method: "POST", redirect: "error", cache: "no-store",
    signal: AbortSignal.timeout(6_000),
    headers: { Authorization: `Bearer ${config.token}`, "Content-Type": "application/json",
      "Cache-Control": "no-store", Accept: "application/json" }, body: JSON.stringify(request) });
  if (!response.ok) throw new Error("Completed candle validation service failed");
  const result = summary.parse(await response.json());
  const candles = completedMarketCandles(series);
  if (result.instrument_id !== id.instrumentId || result.provider !== id.provider ||
      result.exchange !== id.exchange || result.price_type !== id.priceType ||
      result.timeframe_minutes !== id.timeframeMinutes ||
      result.observation_count !== series.observations.length || result.candle_count !== candles.length ||
      result.first_open_time_ms !== (candles[0]?.openTime ?? null) ||
      result.last_open_time_ms !== (candles.at(-1)?.openTime ?? null) ||
      JSON.stringify(result.missing_open_times_ms) !== JSON.stringify(missingCompletedCandleOpenTimes(series)))
    throw new Error("Completed candle validation response does not match request");
  return result;
}
