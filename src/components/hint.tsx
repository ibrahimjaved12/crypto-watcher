import type { ReactNode } from "react";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { GLOSSARY } from "@/lib/labels";

/**
 * Dotted-underline term with a plain-language tooltip. Pass a GLOSSARY key as `term`, or your own
 * `text`. The trigger is a real button so keyboard users can focus it to read the tooltip.
 * Requires the TooltipProvider rendered by AppShell.
 */
export function Hint({
  term,
  text,
  children,
}: {
  term?: string | undefined;
  text?: string | undefined;
  children?: ReactNode;
}) {
  const entry = term ? GLOSSARY[term] : undefined;
  const plain = text ?? entry?.plain;
  const label = children ?? entry?.term ?? term;
  if (!plain) return <>{label}</>;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          className="cursor-help rounded-sm text-left underline decoration-muted-foreground/60 decoration-dotted underline-offset-4"
        >
          {label}
        </button>
      </TooltipTrigger>
      <TooltipContent
        side="top"
        className="max-w-72 whitespace-pre-line border border-border bg-popover text-left text-xs leading-relaxed font-normal text-popover-foreground normal-case tracking-normal"
      >
        {plain}
      </TooltipContent>
    </Tooltip>
  );
}
