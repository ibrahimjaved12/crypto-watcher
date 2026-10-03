import { createFileRoute, Link } from "@tanstack/react-router";
import {
  ArrowUpRight,
  Bell,
  FlaskConical,
  LineChart,
  NotebookPen,
  Orbit,
  ScanLine,
  SlidersHorizontal,
} from "lucide-react";
import { Button } from "@/components/ui/button";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Crypto Watch — Your Market Observatory" },
      {
        name: "description",
        content:
          "A personal workspace for crypto market monitoring, technical snapshots, saved movement alerts and experimental strategy evaluation.",
      },
      { property: "og:title", content: "Crypto Watch — Your Market Observatory" },
      {
        property: "og:description",
        content: "Follow the market. Understand the movement. Build your own perspective.",
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
    title: "Markets in view",
    body: "Follow your pairs with exchange prices, candle charts and movement from 5 minutes to 24 hours.",
  },
  {
    icon: ScanLine,
    title: "A clearer market reading",
    body: "Explore completed-candle snapshots, trend and momentum factors, and the context behind each rule score.",
  },
  {
    icon: Bell,
    title: "Movements worth reviewing",
    body: "Save alerts when prices cross your threshold from a saved baseline, with separate cooldowns for rises and falls.",
  },
  {
    icon: FlaskConical,
    title: "Ideas under observation",
    body: "Forward-evaluate signals and paper trades. Compare hypothetical daily portfolios against matched benchmarks.",
  },
  {
    icon: NotebookPen,
    title: "Your perspective, preserved",
    body: "Keep notes alongside the pairs you follow. Revisit the observations behind your decisions.",
  },
  {
    icon: SlidersHorizontal,
    title: "Monitoring on your terms",
    body: "Choose which monitoring activities are enabled and see whether data is fresh, delayed or unavailable.",
  },
];
function Landing() {
  return (
    <div className="min-h-screen">
      <header className="mx-auto flex max-w-[1320px] items-center justify-between px-5 py-5 sm:px-8">
        <Link to="/" className="flex items-center gap-2.5 font-display text-lg font-semibold">
          <span className="orbit-mark size-10">
            <Orbit className="size-5 text-primary" aria-hidden />
          </span>
          Crypto Watch
        </Link>
        <Button asChild variant="outline">
          <Link to="/auth">
            Sign in <ArrowUpRight className="size-4" aria-hidden />
          </Link>
        </Button>
      </header>
      <main className="mx-auto max-w-[1320px] px-5 pb-12 sm:px-8">
        <section className="observatory panel mt-6 grid items-center gap-12 px-6 py-12 sm:p-12 lg:grid-cols-[1.4fr_1fr] lg:py-20">
          <div>
            <p className="eyebrow">Your personal market observatory</p>
            <h1 className="mt-5 max-w-3xl text-4xl font-semibold leading-[1.1] sm:text-6xl">
              A wider view.
              <br />
              <span className="text-primary">A clearer read.</span>
            </h1>
            <p className="mt-6 max-w-xl text-base leading-relaxed text-muted-foreground sm:text-lg">
              Follow the market, understand its movement and put ideas to the test. One focused
              workspace for your crypto watchlist, analysis and observations.
            </p>
            <Button asChild size="lg" className="mt-8">
              <Link to="/auth">
                Open your workspace <ArrowUpRight className="size-4" aria-hidden />
              </Link>
            </Button>
            <p className="mt-4 text-xs text-muted-foreground">
              Market intelligence and hypothetical evaluation. You make the decisions.
            </p>
          </div>
          <div
            className="relative mx-auto w-full max-w-sm rounded-full border border-primary/15 p-8 sm:p-10"
            aria-hidden="true"
          >
            <div className="rounded-full border border-primary/20 bg-background/40 p-8 text-center">
              <Orbit className="mx-auto size-16 text-primary/80" strokeWidth={1} />
              <p className="mt-5 font-display text-xl font-semibold">
                Observe. Understand.
                <br />
                Evaluate.
              </p>
              <p className="mt-3 text-xs text-muted-foreground">A connected view of your markets</p>
            </div>
            <span className="absolute left-0 top-7 rounded-full border border-primary/30 bg-card px-4 py-2 text-xs text-primary">
              Market overview
            </span>
            <span className="absolute bottom-5 right-0 rounded-full border border-border bg-card px-4 py-2 text-xs">
              Strategy Lab
            </span>
          </div>
        </section>
        <section className="mt-14" aria-labelledby="features-heading">
          <p className="eyebrow">Built for perspective</p>
          <h2 id="features-heading" className="mt-3 text-2xl font-semibold sm:text-3xl">
            From market movement to informed observation.
          </h2>
          <div className="mt-7 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map(({ icon: Icon, title, body }) => (
              <article key={title} className="panel p-6">
                <Icon className="size-5 text-primary" aria-hidden />
                <h3 className="mt-5 text-lg font-semibold">{title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{body}</p>
              </article>
            ))}
          </div>
        </section>
        <footer className="mt-10 flex flex-wrap items-center justify-between gap-4 border-t border-border py-6 text-xs text-muted-foreground">
          <p>Crypto Watch · Personal crypto intelligence</p>
          <p>Experimental strategies. No validated trading edge.</p>
        </footer>
      </main>
    </div>
  );
}
