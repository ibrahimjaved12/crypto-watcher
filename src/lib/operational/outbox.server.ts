import type { OperationalStore, OutboxEvent } from "./types";

export type IdempotentOutboxSink = {
  /** Must atomically/upsert by eventId; the whole batch may be retried after an uncertain response. */
  deliver(events: readonly OutboxEvent[]): Promise<void>;
};

export async function deliverOutboxBatch(
  store: OperationalStore,
  sink: IdempotentOutboxSink,
  workerId: string,
  limit = 50,
) {
  if (!store.enabled) return { claimed: 0, delivered: 0, failed: 0 };
  const events = await store.claimOutbox(workerId, limit);
  if (events.length === 0) return { claimed: 0, delivered: 0, failed: 0 };
  try {
    await sink.deliver(events);
  } catch (error) {
    for (const event of events) {
      await store.markOutboxFailed(
        event.eventId,
        workerId,
        error instanceof Error ? error.message : String(error),
      );
    }
    return { claimed: events.length, delivered: 0, failed: events.length };
  }

  let delivered = 0;
  for (const event of events) {
    try {
      await store.markOutboxDelivered(event.eventId, workerId);
      delivered += 1;
    } catch {
      // The destination already confirmed. Leave the source lease to expire so
      // the stable event ID is retried instead of incorrectly recording failure.
    }
  }
  return { claimed: events.length, delivered, failed: events.length - delivered };
}
