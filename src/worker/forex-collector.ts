import { randomUUID } from "node:crypto";
import {
  FX_DEFAULT_BACKFILL_DAYS,
  createGatewayClient,
  parseFxSubscriptions,
  startFxCollector,
} from "../lib/fx/fx-collector.server";
import { getOperationalStore } from "../lib/operational/repository.server";

// Runnable entrypoint for the forex collector (#453), a forex child of the worker host (off unless
// FX_COLLECTOR_ENABLED=true). It polls the MT5 gateway (#452) and writes the local operational store only; it
// has no hosted credential and no Python dependency. `--subscribe` seeds the instrument set from
// FX_SUBSCRIPTIONS ("fx:EURUSD=EURUSD,fx:XAUUSD=XAUUSD"; the native names are the broker's, supplied by the
// owner) and exits.
const LOG = "[fx-collector]";

try {
  const env = process.env;
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The operational database is disabled (OPERATIONAL_DB_ENABLED=true is required)");

  if (process.argv.includes("--subscribe")) {
    const subscriptions = parseFxSubscriptions(env["FX_SUBSCRIPTIONS"], env["FX_VENUE"]?.trim() || undefined);
    await store.assignFxSubscriptions(subscriptions);
    console.log(`${LOG} assigned ${subscriptions.length} instrument(s): ${subscriptions.map((item) => `${item.instrumentId}=${item.nativeSymbol}`).join(", ")}`);
    process.exit(0);
  }

  const url = env["FX_GATEWAY_URL"]?.trim();
  const token = env["FX_GATEWAY_TOKEN"]?.trim();
  if (!url || !token) throw new Error("FX_GATEWAY_URL and FX_GATEWAY_TOKEN are required");
  const backfillText = env["FX_BACKFILL_DAYS"]?.trim();
  const backfillDays = backfillText ? Number(backfillText) : FX_DEFAULT_BACKFILL_DAYS;
  if (!Number.isInteger(backfillDays) || backfillDays < 0 || backfillDays > 30)
    throw new Error("FX_BACKFILL_DAYS must be a whole number of days between 0 and 30");

  const accountCurrency = (env["FX_ACCOUNT_CURRENCY"]?.trim() || "USD").toUpperCase();
  if (!/^[A-Z]{3}$/.test(accountCurrency)) throw new Error("FX_ACCOUNT_CURRENCY must be a three-letter currency code");

  const collector = startFxCollector({
    store,
    gateway: createGatewayClient({ url, token }),
    now: () => Date.now(),
    instanceId: randomUUID(), // the lease owner id is a UUID
    accountCurrency,
    backfillDays,
    log: (line) => console.log(line),
    error: (line) => console.error(line),
  });
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.once(signal, () => {
      void collector.stop().finally(() => process.exit(0));
    });
  }
} catch (error) {
  console.error(`${LOG} ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
