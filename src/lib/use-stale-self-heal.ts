import { useEffect, useRef, useState } from "react";

/** Data must have been graded stale this long without a break before a self-heal check runs. */
export const SELF_HEAL_STALE_FOR_MS = 20_000;
/** At most one self-heal check this often. */
export const SELF_HEAL_COOLDOWN_MS = 3 * 60_000;

/**
 * `stale` is the underlying data problem (data past its limit or the machine late), not the display grade: a poll
 * or a refetch briefly turns the grade into "catching up", which must neither restart the 20 s wait nor clear
 * `attempted`. Runs one monitor check on the user's behalf when TA or the movement reading has stayed stale for
 * SELF_HEAL_STALE_FOR_MS while the tab is visible and `allowed` (monitoring on, no resume settle, no manual
 * check pending), at most once per SELF_HEAL_COOLDOWN_MS. `run` reports its own failures; success is silent.
 * `healing` is true while the check runs (the grades then read "catching up"); `attempted` stays true once a
 * check was tried, so the page can say the data is still catching up after it.
 */
export function useStaleSelfHeal({
  stale,
  allowed,
  run,
}: {
  stale: boolean;
  allowed: boolean;
  run: () => Promise<unknown>;
}): { healing: boolean; attempted: boolean } {
  const [healing, setHealing] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState === "visible");
  const lastAttemptAt = useRef<number | null>(null);
  const runRef = useRef(run);
  runRef.current = run;

  useEffect(() => {
    const onVisibility = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  useEffect(() => {
    if (!stale) setAttempted(false);
  }, [stale]);

  useEffect(() => {
    if (!stale || !allowed || !visible || healing) return;
    const cooldownLeft =
      lastAttemptAt.current === null ? 0 : lastAttemptAt.current + SELF_HEAL_COOLDOWN_MS - Date.now();
    const timer = setTimeout(() => {
      lastAttemptAt.current = Date.now();
      setAttempted(true);
      setHealing(true);
      void runRef.current().finally(() => setHealing(false));
    }, Math.max(SELF_HEAL_STALE_FOR_MS, cooldownLeft));
    return () => clearTimeout(timer);
  }, [stale, allowed, visible, healing]);

  return { healing, attempted };
}
