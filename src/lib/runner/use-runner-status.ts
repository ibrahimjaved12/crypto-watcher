import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerFn } from "@tanstack/react-start";
import { getRunnerStatus } from "./status.functions";

export function useRunnerStatus() {
  const queryClient = useQueryClient();
  const load = useServerFn(getRunnerStatus);
  return useQuery({
    queryKey: ["runner-status"],
    queryFn: async () => {
      const status = await load();
      const previous = queryClient.getQueryData<{ lastOnlineAt: number | null }>(["runner-status"]);
      return {
        ...status,
        lastOnlineAt: status.online === true ? Date.now() : (previous?.lastOnlineAt ?? null),
      };
    },
    refetchInterval: (query) => (query.state.data?.configured === true ? 30_000 : false),
    staleTime: 15_000,
  });
}
