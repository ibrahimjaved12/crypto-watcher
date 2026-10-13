import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { getRunnerStatus } from "./status.functions";

export function useRunnerStatus() {
  const load = useServerFn(getRunnerStatus);
  const query = useQuery({
    queryKey: ["runner-status"],
    queryFn: () => load(),
    refetchInterval: (query) => (query.state.data?.configured === false ? false : 30_000),
    retry: false,
    staleTime: 15_000,
  });
  // Missing health must never enable controls using saved results or old query data.
  const status = query.isError ? undefined : query.data;
  const checkFailed = query.isError || status?.reason === "check-failed";
  const available = !checkFailed && !!status && (!status.configured || status.online === true);
  const serviceAvailable = (service: keyof NonNullable<typeof status>["services"]) =>
    available && (status?.configured === false || status?.services[service] === true);
  return {
    ...query,
    available,
    checkFailed,
    canAnalyse: serviceAvailable("analysis"),
    canCheckAlerts: serviceAvailable("alerts"),
    canRunForward: serviceAvailable("forward"),
    canQueueJobs: serviceAvailable("jobs"),
  };
}
