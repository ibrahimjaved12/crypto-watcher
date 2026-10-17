import { Bitcoin, Globe, Lock } from "lucide-react";
import { Fragment, useRef, type KeyboardEvent, type ReactElement } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { MARKETS, type Market } from "@/lib/market/instruments";
import { useMarket } from "@/lib/market/market-context";
import { MARKET_META } from "@/lib/market/market-select";
import { cn } from "@/lib/utils";

const ICONS = { bitcoin: Bitcoin, globe: Globe } as const;

/**
 * Crypto/Forex segmented control (#374). Desktop: a compact 36 px pill in the header row. Mobile: its own
 * full-width 44 px row, always icon + label. A market the user may not open stays visible but locked; activating
 * it opens a short explanation and never switches.
 */
export function MarketToggle({ variant, className }: { variant: "desktop" | "mobile"; className?: string }) {
  const { market, setMarket, canOpen } = useMarket();
  const refs = useRef<Record<Market, HTMLButtonElement | null>>({ crypto: null, forex: null });
  const mobile = variant === "mobile";
  const index = MARKETS.indexOf(market);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const focused = MARKETS.findIndex((m) => refs.current[m] === document.activeElement);
    const from = focused < 0 ? index : focused;
    const to = (from + (event.key === "ArrowRight" ? 1 : -1) + MARKETS.length) % MARKETS.length;
    refs.current[MARKETS[to]!]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-label="Market"
      onKeyDown={onKeyDown}
      className={cn(
        "relative grid grid-cols-2 rounded-full border border-border/70 bg-background/40 p-0.5",
        mobile ? "h-11 w-full" : "h-9 w-[168px] shrink-0",
        className,
      )}
    >
      <span
        aria-hidden
        className="market-toggle__thumb pointer-events-none absolute inset-y-0.5 left-0.5 w-[calc(50%-2px)] rounded-full border border-primary/50 bg-primary/15 shadow-[0_0_14px_-4px_var(--color-primary)]"
        style={{ transform: `translateX(${index * 100}%)` }}
      />
      {MARKETS.map((m) => {
        const meta = MARKET_META[m];
        const locked = !canOpen(m);
        const active = m === market;
        const Icon = locked ? Lock : ICONS[meta.icon];
        const button = (
          <button
            ref={(node) => {
              refs.current[m] = node;
            }}
            type="button"
            role="radio"
            aria-checked={active}
            aria-disabled={locked || undefined}
            tabIndex={active ? 0 : -1}
            onClick={locked ? undefined : () => setMarket(m)}
            className={cn(
              "relative z-10 flex items-center justify-center gap-1.5 rounded-full font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
              mobile ? "min-h-11 text-sm" : "text-sm",
              active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              locked && "border border-dashed border-border opacity-55",
            )}
          >
            <Icon className={locked ? "size-3.5" : "size-4"} aria-hidden />
            <span>{meta.label}</span>
            {locked ? <span className="sr-only">(locked)</span> : null}
          </button>
        );
        if (locked) return <LockedHalf key={m}>{button}</LockedHalf>;
        if (mobile) return <Fragment key={m}>{button}</Fragment>;
        return (
          <Tooltip key={m}>
            <TooltipTrigger asChild>{button}</TooltipTrigger>
            <TooltipContent>{meta.tagline}</TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );
}

function LockedHalf({ children }: { children: ReactElement }) {
  return (
    <Popover>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent align="center" className="w-[min(18rem,calc(100vw-2rem))] rounded-2xl border-border panel">
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-muted ring-1 ring-border">
            <Lock className="size-4 text-muted-foreground" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="font-medium">Forex is not on your plan yet</p>
            <p className="mt-1 text-sm text-muted-foreground">Your crypto tools are unchanged.</p>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
