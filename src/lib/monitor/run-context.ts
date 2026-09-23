import {
  loadObservationCandles,
  loadTACandles,
  type Candle,
  type ProviderObserver,
  type ProviderOutcome,
  type TACandleResult,
} from "../market/providers.server";

export type MonitorMetrics = {
  exchangeRequests: number;
  candleRows: number;
  marketCacheHits: number;
  taCalculations: number;
  taSignalsSaved: number;
  taOutcomesUpdated: number;
  databaseReads: number;
  databaseWriteAttempts: number;
  databaseNoOps: number;
};

export type MonitorRunContext = {
  metrics: MonitorMetrics;
  observation(
    symbol: string,
    validate?: (result: Extract<ProviderOutcome, { ok: true }>["result"]) => void,
  ): Promise<ProviderOutcome>;
  ta(
    symbol: string,
    timeframe: number,
    validate: (candles: Candle[]) => void,
    requestedSource?: string,
  ): Promise<TACandleResult>;
};

export function emptyMonitorMetrics(): MonitorMetrics {
  return {
    exchangeRequests: 0,
    candleRows: 0,
    marketCacheHits: 0,
    taCalculations: 0,
    taSignalsSaved: 0,
    taOutcomesUpdated: 0,
    databaseReads: 0,
    databaseWriteAttempts: 0,
    databaseNoOps: 0,
  };
}

export function metricsSnapshot(metrics: MonitorMetrics): MonitorMetrics {
  return { ...metrics };
}

export function metricsSince(current: MonitorMetrics, previous: MonitorMetrics): MonitorMetrics {
  return Object.fromEntries(
    Object.keys(current).map((key) => [
      key,
      current[key as keyof MonitorMetrics] - previous[key as keyof MonitorMetrics],
    ]),
  ) as MonitorMetrics;
}

/**
 * Request-scoped market state. Cache keys contain every input that can change the
 * meaning of a candle series; results never outlive one manual or scheduled run.
 */
export function createMonitorRunContext(): MonitorRunContext {
  const metrics = emptyMonitorMetrics();
  const observations = new Map<string, Promise<ProviderOutcome>>();
  const taSeries = new Map<string, Promise<TACandleResult>>();
  const observer: ProviderObserver = {
    request() {
      metrics.exchangeRequests += 1;
    },
    candleRows(_source, _timeframe, rows) {
      metrics.candleRows += rows;
    },
  };

  return {
    metrics,
    async observation(symbol, validate) {
      // The current observation contract is a futures trade-price 1m series with
      // the documented provider fallback order. Keep those semantics in the key.
      const key = `futures:trade:fallback:${symbol}:1m`;
      let pending = observations.get(key);
      if (pending) metrics.marketCacheHits += 1;
      else {
        pending = loadObservationCandles(symbol, undefined, observer);
        observations.set(key, pending);
      }
      const outcome = await pending;
      if (outcome.ok) validate?.(outcome.result);
      return outcome;
    },
    async ta(symbol, timeframe, validate, requestedSource) {
      const sourceKey = requestedSource ?? "fallback";
      const key = `futures:trade:${sourceKey}:${symbol}:${timeframe}m`;
      let pending = taSeries.get(key);
      if (pending) metrics.marketCacheHits += 1;
      else {
        pending = loadTACandles(symbol, timeframe, validate, requestedSource, observer);
        taSeries.set(key, pending);
        if (!requestedSource) {
          void pending
            .then((result) => {
              const resolvedKey = `futures:trade:${result.source}:${symbol}:${timeframe}m`;
              if (!taSeries.has(resolvedKey)) taSeries.set(resolvedKey, Promise.resolve(result));
            })
            .catch(() => undefined);
        }
      }
      const result = await pending;
      validate(result.candles);
      return result;
    },
  };
}
