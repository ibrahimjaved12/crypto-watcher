import { supabaseAdmin } from "../integrations/supabase/client.server";
import { syncCollectorUniverse } from "../lib/market/collector-subscriptions.server";
import { collectorLogMessage } from "../lib/market/collector-logging";
import {
  collectorUniverseSyncConfigured,
  startCollectorUniverseSync,
} from "../lib/market/collector-universe-sync";
import { getOperationalStore } from "../lib/operational/repository.server";

// Machine-only sibling of the collector and quote publisher. All runtime
// dependencies are loaded statically before Vite's module runner closes (#327).
try {
  if (!collectorUniverseSyncConfigured(process.env)) {
    console.info(
      "[collector-universe-sync] not started: disabled or hosted/operational configuration missing",
    );
  } else {
    const store = getOperationalStore();
    const sync = startCollectorUniverseSync({
      sync: () => syncCollectorUniverse(supabaseAdmin, store),
    });
    for (const signal of ["SIGTERM", "SIGINT"] as const) {
      process.once(signal, () => {
        sync.stop();
        process.exit(0);
      });
    }
  }
} catch (error) {
  console.error(`[collector-universe-sync] ${collectorLogMessage(error).slice(0, 200)}`);
  process.exit(1);
}
