import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";

import { getForwardDashboard } from "@/lib/forward.functions";
import { forwardDataStale } from "@/lib/forward/forward-freshness";

/** Shares the Strategy Lab's existing hosted/RLS read and query cache. */
export function ForwardFreshnessBanner() {
  const load = useServerFn(getForwardDashboard);
  const data = useQuery({
    queryKey: ["forward-dashboard"],
    queryFn: () => load(),
    refetchInterval: 60_000,
  });
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  const boundary = data.data?.latestRun?.boundary_ms;
  const stale = forwardDataStale(boundary, now);
  if (!stale && !data.isError && (data.isPending || boundary != null)) return null;

  return (
    <div role="status" className="my-4 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm">
      {stale ? (
        <>
          <strong>Forward data is stale.</strong> The latest hosted run is more than two hours
          behind (through {new Date(boundary!).toLocaleString()}). The worker host may be down
          or catching up. Results remain available, but may be out of date.
        </>
      ) : data.isError ? (
        <>Forward data freshness is unavailable. The hosted read failed; try again shortly.</>
      ) : (
        <>No hosted forward run is available yet. The worker host may still be starting or catching up.</>
      )}
    </div>
  );
}
