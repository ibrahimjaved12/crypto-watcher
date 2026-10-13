import { useQuery } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { getRunnerStatus } from "./status.functions";

export function useRunnerStatus() {
  const load = useServerFn(getRunnerStatus);
  return useQuery({
    queryKey: ["runner-status"],
    queryFn: () => load(),
    refetchInterval: (query) => (query.state.data?.configured === true ? 30_000 : false),
    staleTime: 15_000,
  });
}
