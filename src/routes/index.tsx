import { createFileRoute, Link } from "@tanstack/react-router";
import { Activity, Bell, Clock, FlaskConical, LineChart, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        name: "description",
        content:
          "A personal crypto futures watcher with alerts and a cost-realistic paper-trading lab. Place real orders manually.",
      },
      { property: "og:title", content: "Crypto Watch — Personal Crypto Market Monitor" },
      {
        property: "og:description",
        content:
          "Follow crypto futures, save alerts and test strategies with fake money and realistic fees.",
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
    body: "See prices, a 24-hour chart and moves from five minutes to a day, all in one place.",
  },
  {
    icon: Clock,
    title: "Runs without you",
    body: "Scheduled checks keep watching your pairs even when this tab is closed.",
  },
  {
    icon: Bell,
    title: "Threshold alerts",
    body: "Your own percentage threshold and time window, with a cooldown so you are not spammed.",
  },
  {
    icon: FlaskConical,
    title: "Test before you trust",
    body: "A paper-trading lab includes fees and funding costs. No strategy has a validated edge yet.",
  },
  {
    icon: ShieldCheck,
    title: "Yours only",
    body: "Your alerts and notes belong to your account. You place every real order manually.",
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
          Your personal crypto futures watcher
        </p>
        <h1 className="mt-4 max-w-3xl text-4xl font-semibold leading-tight sm:text-5xl">
          See the moves. Understand the signals.
        </h1>
        <p className="mt-5 max-w-2xl text-lg text-muted-foreground">
          Follow crypto futures with clear alerts and a cost-realistic paper-trading lab. You decide
          what to trade and place real orders manually.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Button asChild size="lg">
            <Link to="/auth">Open your watchlist</Link>
          </Button>
        </div>

        <p className="mt-5 max-w-xl text-sm text-muted-foreground">
          The lab uses fake money. Scores describe indicators; they do not predict profit.
        </p>
        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
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
