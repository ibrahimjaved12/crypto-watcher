import { createFileRoute, Link } from "@tanstack/react-router";
import { Activity, Bell, FlaskConical, NotebookPen, LineChart, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        name: "description",
        content:
          "Market monitoring, technical snapshots, saved movement alerts and experimental strategy evaluation in one private workspace.",
      },
      { property: "og:title", content: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        property: "og:description",
        content:
          "Private watchlists, real exchange data, technical snapshots and strategy forward evaluation.",
      },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: Landing,
});

const FEATURES = [
  {
    icon: LineChart,
    title: "Live market view",
    body: "Real public exchange prices and candles, with 5m, 15m, 1h, 4h and 24h changes per pair.",
  },
  {
    icon: FlaskConical,
    title: "A lab for your hypotheses",
    body: "Follow strategy signals, paper trading and hypothetical daily portfolios. Experimental evaluation, with no validated trading edge.",
  },
  {
    icon: Bell,
    title: "Threshold alerts",
    body: "Track rises and falls from a saved baseline, with your own threshold and a separate cooldown for each direction.",
  },
  {
    icon: NotebookPen,
    title: "Keep the context",
    body: "Build a private record of market observations, with notes linked to the pairs you follow.",
  },
  {
    icon: ShieldCheck,
    title: "Decisions stay with you",
    body: "Review technical snapshots from completed candles. Rule scores describe market conditions; they are not win probabilities.",
  },
];

function Landing() {
  return (
    <div className="min-h-screen orbital-hero">
      <header className="mx-auto flex max-w-7xl items-center px-4 py-5">
        <span className="flex items-center gap-2 font-display text-lg font-semibold">
          <Activity className="size-5 text-primary" aria-hidden />
          Crypto Watch
        </span>
        <Button asChild size="sm" className="ml-auto">
          <Link to="/auth">Sign in</Link>
        </Button>
      </header>

      <section className="mx-auto max-w-7xl px-4 pb-16 pt-10 sm:pt-20">
        <p className="text-xs font-medium uppercase tracking-[0.2em] text-primary">
          Your market observatory
        </p>
        <h1 className="mt-4 max-w-3xl text-4xl font-semibold leading-tight sm:text-6xl">
          A clearer view of the market. Space to think ahead.
        </h1>
        <p className="mt-5 max-w-2xl text-lg text-muted-foreground">
          Watch your markets, understand technical snapshots, and evaluate ideas as new data
          arrives. Your watchlist, movement alerts, notes and Strategy Lab — in one focused
          workspace.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Button asChild size="lg">
            <Link to="/auth">Open your observatory</Link>
          </Button>
        </div>

        <div className="mt-16 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <div key={title} className="panel p-5">
              <Icon className="size-5 text-primary" aria-hidden />
              <h2 className="mt-3 text-base font-semibold">{title}</h2>
              <p className="mt-1.5 text-sm text-muted-foreground">{body}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
