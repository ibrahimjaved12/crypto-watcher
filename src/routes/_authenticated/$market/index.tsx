import { createFileRoute, redirect } from "@tanstack/react-router";

// /crypto and /forex open the market's dashboard.
export const Route = createFileRoute("/_authenticated/$market/")({
  beforeLoad: ({ params }) => {
    throw redirect({ to: "/$market/dashboard", params: { market: params.market }, replace: true });
  },
});
