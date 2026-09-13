import { createFileRoute, Link } from "@tanstack/react-router";
import { Activity, Bell, Clock, Database, LineChart, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        name: "description",
        content:
          "Track crypto pairs, watch 5m to 24h price moves, and get threshold alerts saved to your private history. Monitoring keeps running when your browser is closed.",
      },
      { property: "og:title", content: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        property: "og:description",
        content:
          "Private watchlists, real exchange data, scheduled price monitoring and searchable alert history.",
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
    icon: Clock,
    title: "Runs without you",
    body: "A scheduled backend check every 5 minutes — no browser tab required.",
  },
  {
    icon: Bell,
    title: "Threshold alerts",
    body: "Your own percentage threshold and time window, with a cooldown so you are not spammed.",
  },
  {
    icon: Database,
    title: "Kept in a database",
    body: "Watchlists, settings, alerts and notes are stored per account — not in your browser.",
  },
  {
    icon: ShieldCheck,
    title: "Yours only",
    body: "Every record is locked to your account. Manual trading only — nothing is ever executed.",
  },
];

function Landing() {
  return (
    <div className="min-h-screen">
      <header className="mx-auto flex max-w-6xl items-center px-4 py-5">
        <span className="flex items-center gap-2 font-display text-lg font-semibold">
          <Activity className="size-5 text-primary" aria-hidden />
          Crypto Watch
        </span>
        <Button asChild size="sm" className="ml-auto">
          <Link to="/auth">Sign in</Link>
        </Button>
      </header>

      <section className="mx-auto max-w-6xl px-4 pb-16 pt-10 sm:pt-20">
        <p className="num text-xs uppercase tracking-[0.2em] text-primary">
          personal market monitoring
        </p>
        <h1 className="mt-4 max-w-3xl text-4xl font-semibold leading-tight sm:text-6xl">
          Watch the market move. Decide the trades yourself.
        </h1>
        <p className="mt-5 max-w-2xl text-lg text-muted-foreground">
          Crypto Watch follows your pairs on real exchange data, checks them on a schedule, and
          records every threshold crossing with the exact rule and data source behind it.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Button asChild size="lg">
            <Link to="/auth">Get started</Link>
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
