import { supabaseAdmin } from "../integrations/supabase/client.server";
import { createHostedFxPublish, startFxQuotePublisher } from "../lib/fx/fx-quote-publisher.server";
import { getOperationalStore } from "../lib/operational/repository.server";

// Runnable entrypoint for the forex quote publisher (#433), a non-essential child of the worker host.
// It reads only the local operational store (subscriptions, completed 1m BID bars, symbol specs) and
// publishes to the hosted fx_market_quotes with the service-role key; it makes no broker call.
try {
  const store = getOperationalStore();
  if (!store.enabled) throw new Error("The operational database is disabled (OPERATIONAL_DB_ENABLED=true is required)");
  const publisher = startFxQuotePublisher({ deps: { store, publish: createHostedFxPublish(supabaseAdmin) } });
  if (!publisher) process.exit(1);
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.once(signal, () => {
      publisher.stop();
      process.exit(0);
    });
  }
} catch (error) {
  console.error(`[fx-quote] ${error instanceof Error ? error.message : String(error)}`);
  process.exit(1);
}
