import type { ReactNode } from "react";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { GLOSSARY } from "@/lib/labels";

/**
 * A term with a dotted underline that explains itself in plain words. A popover (not a hover
 * tooltip) so it also works with a tap on phones and with the keyboard.
 */
export function Hint({
  term,
  children,
  text,
}: {
  term?: string | undefined;
  children?: ReactNode;
  text?: string | undefined;
}) {
  const entry = term ? GLOSSARY[term] : undefined;
  const plain = text ?? entry?.plain;
  const label = children ?? entry?.term ?? term;
  if (!plain) return <>{label}</>;
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          className="cursor-help rounded-sm text-left underline decoration-muted-foreground/60 decoration-dotted underline-offset-4 hover:decoration-primary"
        >
          {label}
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-72 text-xs leading-relaxed" side="top">
        {entry && <p className="mb-1 font-medium text-foreground">{entry.term}</p>}
        <p className="text-muted-foreground">{plain}</p>
      </PopoverContent>
    </Popover>
  );
}
