import { useEffect, useState } from "react";

/**
 * Keep elapsed-time labels current even when query data has not changed.
 *
 * With `hold`, the returned time stays frozen (the timer keeps running but does not publish), and the moment
 * hold turns false it jumps to the real time. The dashboard holds while a resumed tab's refetch is in flight,
 * so a throttled background timer never ages old data past its limit before the new data lands.
 */
export function useNow(intervalMs = 30_000, options: { hold?: boolean } = {}): number {
  const hold = options.hold ?? false;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (hold) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs, hold]);
  return now;
}
