import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { GLOSSARY } from "@/lib/labels";

export function Hint({ term, children }: { term: string; children?: ReactNode }) {
  const entry = GLOSSARY[term];
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="cursor-help rounded-sm text-left underline decoration-dotted underline-offset-4 focus-visible:outline-2 focus-visible:outline-ring"
            aria-label={`${entry?.term ?? term}: explanation`}
          >
            {children ?? entry?.term ?? term}
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-72 text-sm leading-relaxed">
          {entry?.plain ?? "No explanation available yet."}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
