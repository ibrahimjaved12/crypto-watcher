import { createFileRoute, Link } from "@tanstack/react-router";
import { Bell, FlaskConical, Gauge, LineChart, Orbit, ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Crypto Watch — Personal Crypto Futures Watcher" },
      {
        name: "description",
        content:
          "A personal crypto futures watcher: live moves, threshold alerts that run without you, and a cost-realistic paper-trading lab. Real orders stay manual.",
      },
      { property: "og:title", content: "Crypto Watch — Personal Crypto Futures Watcher" },
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
    title: "Live futures view",
    body: "Binance USDⓈ-M perpetual prices with 5m to 24h moves, a 24h chart and a plain-language market read per pair.",
  },
  {
    icon: Bell,
    title: "Alerts that run without you",
    body: "Your own move threshold and quiet time, checked on a schedule even when the browser is closed.",
  },
  {
    icon: FlaskConical,
    title: "Strategy Lab",
    body: "Strategies are paper-traded with fake money, real fees, funding and liquidation, and compared with random entries.",
  },
  {
    icon: Gauge,
    title: "Honest numbers",
    body: "Scores are rankings, not win probabilities. Results count only after costs, and unclear cases count against you.",
  },
  {
    icon: ShieldCheck,
    title: "You place the orders",
    body: "Nothing is ever executed on an exchange. Watchlists, alerts and notes are private to your account.",
  },
];

function Landing() {
  return (
    <div className="min-h-screen overflow-x-hidden">
      <header className="mx-auto flex max-w-6xl items-center px-4 py-5">
        <span className="flex items-center gap-2 font-display text-lg font-semibold">
          <span className="grid size-8 place-items-center rounded-full bg-gradient-to-br from-primary/40 to-aurora/30 ring-1 ring-primary/40">
            <Orbit className="size-4" aria-hidden />
          </span>
          Crypto Watch
        </span>
        <Button asChild className="ml-auto">
          <Link to="/auth">Sign in</Link>
        </Button>
      </header>

      <section className="relative mx-auto max-w-6xl px-4 pb-16 pt-10 sm:pt-20">
        <OrbitArt />
        <p className="eyebrow">Personal crypto futures watcher</p>
        <h1 className="text-nebula mt-4 max-w-3xl text-4xl font-semibold leading-tight sm:text-6xl">
          Watch the market move. Decide the trades yourself.
        </h1>
        <p className="mt-5 max-w-2xl text-lg text-muted-foreground">
          Crypto Watch follows your futures pairs on real exchange data, alerts you when they move,
          and tests trading strategies in a cost-realistic paper-trading lab. Real orders are always
          placed by you, manually.
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Button asChild size="lg">
            <Link to="/auth">Get started</Link>
          </Button>
        </div>

        <div className="mt-16 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {FEATURES.map(({ icon: Icon, title, body }) => (
            <div key={title} className="panel p-5 transition-colors hover:border-primary/40">
              <span className="grid size-9 place-items-center rounded-lg bg-primary/15 ring-1 ring-primary/30">
                <Icon className="size-4 text-primary" aria-hidden />
              </span>
              <h2 className="mt-3 text-base font-semibold">{title}</h2>
              <p className="mt-1.5 text-sm text-muted-foreground">{body}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

/** Decorative orbit rings: small static SVG (no animation), hidden from assistive tech. */
function OrbitArt() {
  return (
    <svg
      viewBox="0 0 400 400"
      className="pointer-events-none absolute -right-24 -top-6 -z-10 hidden w-[28rem] opacity-60 md:block"
      aria-hidden
    >
      <defs>
        <radialGradient id="planet" cx="35%" cy="35%" r="65%">
          <stop offset="0%" stopColor="var(--color-aurora)" stopOpacity="0.9" />
          <stop offset="60%" stopColor="var(--color-primary)" stopOpacity="0.55" />
          <stop offset="100%" stopColor="var(--color-background)" stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="200" cy="200" r="60" fill="url(#planet)" />
      <ellipse cx="200" cy="200" rx="150" ry="52" fill="none" stroke="var(--color-primary)" strokeOpacity="0.35" transform="rotate(-18 200 200)" />
      <ellipse cx="200" cy="200" rx="190" ry="80" fill="none" stroke="var(--color-aurora)" strokeOpacity="0.2" strokeDasharray="2 6" transform="rotate(-18 200 200)" />
      <circle cx="342" cy="152" r="4" fill="var(--color-aurora)" />
      <circle cx="58" cy="246" r="2.5" fill="var(--color-primary)" />
    </svg>
  );
}
