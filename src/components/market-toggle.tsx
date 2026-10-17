import { Bitcoin, Globe, Lock } from "lucide-react";
import { Fragment, useRef, type KeyboardEvent, type ReactElement } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { MARKETS, type Market } from "@/lib/market/instruments";
import { useMarket } from "@/lib/market/market-context";
import { MARKET_META } from "@/lib/market/market-select";
import { cn } from "@/lib/utils";

const ICONS = { bitcoin: Bitcoin, globe: Globe } as const;

/** True on devices with a hovering pointer; touch screens skip the tagline tooltip. */
function canHover(): boolean {
  try {
    return window.matchMedia("(hover: hover)").matches;
  } catch {
    return false;
  }
}

/**
 * Crypto/Forex segmented control (#374, #413). One fixed size everywhere (labels never change its width, so nothing
 * around it moves); always icon + label. A market the user may not open stays visible but locked; activating it
 * opens a short explanation and never switches.
 */
export function MarketToggle({ className }: { className?: string }) {
  const { market, setMarket, canOpen } = useMarket();
  const refs = useRef<Record<Market, HTMLButtonElement | null>>({ crypto: null, forex: null });
  const index = MARKETS.indexOf(market);
  const hover = canHover();

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
        "relative grid h-9 w-[132px] grid-cols-2 items-stretch rounded-full border border-border/70 bg-background/50 p-[3px] sm:w-[148px]",
        className,
      )}
    >
      <span
        aria-hidden
        className="market-toggle__thumb pointer-events-none absolute inset-y-[3px] left-[3px] w-[calc((100%-6px)/2)] rounded-full bg-primary/15 shadow-[0_0_12px_-4px_var(--color-primary)] ring-1 ring-inset ring-primary/45"
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
              "relative z-10 flex h-full items-center justify-center gap-1.5 rounded-full text-[13px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-ring",
              active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
              locked && "border border-dashed border-border opacity-60",
            )}
          >
            <Icon className="size-3.5 shrink-0" aria-hidden />
            <span>{meta.label}</span>
            {locked ? <span className="sr-only">(locked)</span> : null}
          </button>
        );
        if (locked) return <LockedHalf key={m} market={m}>{button}</LockedHalf>;
        if (!hover) return <Fragment key={m}>{button}</Fragment>;
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

function LockedHalf({ market, children }: { market: Market; children: ReactElement }) {
  return (
    <Popover>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent align="center" className="w-[min(18rem,calc(100vw-2rem))] rounded-2xl border-border panel">
        <div className="flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-muted ring-1 ring-border">
            <Lock className="size-4 text-muted-foreground" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="font-medium">{`${MARKET_META[market].label} isn't switched on for you`}</p>
            <p className="mt-1 text-sm text-muted-foreground">Ask the owner to add it.</p>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}
