import { supabaseAdmin } from "../integrations/supabase/client.server";
import { createHostedPublish, startMarketQuotePublisher } from "../lib/market/quote-publisher.server";
import { getOperationalStore } from "../lib/operational/repository.server";

// Runnable entrypoint for the hosted market quote publisher (#323). It is a separate child of the
// worker host, not part of the collector: it publishes to the main hosted project with the service-role
// key, and the collector artifact must hold no main-database credential. It reads only the local
// operational store and makes no exchange call.
try {
  const publisher = startMarketQuotePublisher({
    deps: { store: getOperationalStore(), publish: createHostedPublish(supabaseAdmin) },
  });
  if (!publisher) process.exit(1);
  for (const signal of ["SIGTERM", "SIGINT"] as const) {
    process.once(signal, () => {
      publisher.stop();
      process.exit(0);
    });
  }
} catch (error) {
  console.error(
    `[market-quote-publisher] ${error instanceof Error ? error.message : String(error)}`,
  );
  process.exit(1);
}
