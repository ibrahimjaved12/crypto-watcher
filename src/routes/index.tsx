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
    body: "Real Binance futures prices for the pairs you watch, with 5-minute to 24-hour changes at a glance.",
  },
  {
    icon: FlaskConical,
    title: "A paper-trading lab",
    body: "Strategies trade fake money with realistic fees and funding, next to a random baseline. Nothing here has a proven edge yet.",
  },
  {
    icon: Bell,
    title: "Move alerts",
    body: "Get an alert when a pair rises or falls past your threshold, with a cooldown so you are not spammed.",
  },
  {
    icon: NotebookPen,
    title: "Keep the context",
    body: "A private journal of what you saw and why, linked to the pairs you follow.",
  },
  {
    icon: ShieldCheck,
    title: "You place the orders",
    body: "Crypto Watch never trades real money. Indicator scores describe the market; they are not win probabilities.",
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

      <section className="relative mx-auto max-w-7xl px-4 pb-16 pt-10 sm:pt-20">
        <svg
          viewBox="0 0 200 200"
          className="pointer-events-none absolute right-4 top-6 hidden size-64 text-primary lg:block"
          aria-hidden
        >
          <defs>
            <radialGradient id="planet" cx="35%" cy="35%" r="70%">
              <stop offset="0%" stopColor="oklch(0.85 0.1 290)" />
              <stop offset="60%" stopColor="oklch(0.5 0.16 290)" />
              <stop offset="100%" stopColor="oklch(0.2 0.06 285)" />
            </radialGradient>
          </defs>
          <ellipse cx="100" cy="100" rx="92" ry="30" fill="none" stroke="currentColor" strokeOpacity="0.35" transform="rotate(-18 100 100)" />
          <circle cx="100" cy="100" r="38" fill="url(#planet)" />
          <path d="M 12 128 A 92 30 -18 0 0 188 72" fill="none" stroke="currentColor" strokeOpacity="0.7" transform="rotate(0 100 100)" />
          <circle cx="178" cy="58" r="3" fill="oklch(0.82 0.12 200)" />
        </svg>
        <p className="text-xs font-medium uppercase tracking-[0.2em] text-primary">
          Your personal crypto futures observatory
        </p>
        <h1 className="mt-4 max-w-3xl text-4xl font-semibold leading-tight sm:text-6xl">
          Watch the market move. <span className="glow-text">Decide the trades yourself.</span>
        </h1>
        <p className="mt-5 max-w-2xl text-lg text-muted-foreground">
          A personal watcher for crypto futures: live prices and alerts for your pairs, plain-
          language indicator readings, and a cost-realistic paper-trading lab to test strategies.
          Real orders are always placed by you, manually.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Button asChild size="lg">
            <Link to="/auth">Open Crypto Watch</Link>
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
