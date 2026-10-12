import { useRunnerStatus } from "@/lib/runner/use-runner-status";

export function RunnerStatusNotice() {
  const { data } = useRunnerStatus();
  if (!data?.configured || data.online !== false) return null;

  return (
    <p role="status" className="mb-2 text-xs text-muted-foreground">
      Runner offline: the computer that does the heavy work is not reachable. Alerts, analysis
      and forward runs will work again when it is back.
    </p>
  );
}
