import { useEffect, useRef, useState } from "react";
import { useQueryClient, type QueryKey } from "@tanstack/react-query";

/** A tab hidden for longer than this refetches on return before any age is judged. */
export const RESUME_HIDDEN_MIN_MS = 5_000;

/**
 * True while a tab that was hidden for more than RESUME_HIDDEN_MIN_MS waits for its fresh data. On return it
 * refetches the given queries at once and settles when none of them is fetching, or after `maxMs`. Pair it with
 * `useNow(…, { hold: settling })`: browsers throttle timers in hidden tabs, so without the hold every age would
 * jump past its limit for the moment between the tab becoming visible and the refetch landing.
 */
export function useResumeSettle(queryKeys: readonly QueryKey[], maxMs = 8_000): boolean {
  const queryClient = useQueryClient();
  const [settling, setSettling] = useState(false);
  const keysRef = useRef(queryKeys);
  keysRef.current = queryKeys;

  useEffect(() => {
    let hiddenAt: number | null = document.visibilityState === "hidden" ? Date.now() : null;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    let unsubscribe: (() => void) | undefined;
    const finish = () => {
      clearTimeout(deadline);
      deadline = undefined;
      unsubscribe?.();
      unsubscribe = undefined;
      setSettling(false);
    };
    const fetching = () =>
      keysRef.current.some((queryKey) => queryClient.isFetching({ queryKey }) > 0);
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
        return;
      }
      const wasHidden = hiddenAt;
      hiddenAt = null;
      if (wasHidden === null || Date.now() - wasHidden <= RESUME_HIDDEN_MIN_MS) return;
      finish();
      setSettling(true);
      deadline = setTimeout(finish, maxMs);
      unsubscribe = queryClient.getQueryCache().subscribe(() => {
        if (!fetching()) finish();
      });
      void Promise.allSettled(
        keysRef.current.map((queryKey) => queryClient.refetchQueries({ queryKey, type: "active" })),
      ).then(() => {
        if (!fetching()) finish();
      });
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      clearTimeout(deadline);
      unsubscribe?.();
    };
  }, [queryClient, maxMs]);

  return settling;
}
