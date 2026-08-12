import { useMemo, useState } from "react";
import { DropdownCaret } from "../ui/DropdownCaret";
import { Markdown } from "./Markdown";

interface ThinkingBlockProps {
  content: string;
  messageId: string;
}

/** Convert single newlines to paragraph breaks for readable spacing */
function preserveLineBreaks(text: string): string {
  return text.replace(/(?<!\n)\n(?!\n)/g, "\n\n");
}

export function ThinkingBlock({ content, messageId }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(true);
  const processed = useMemo(() => preserveLineBreaks(content), [content]);

  if (!content?.trim()) return null;

  return (
    <div className="my-2 rounded-lg border border-border bg-muted/30">
      <div
        className="flex cursor-pointer items-center gap-2 px-3 py-2 text-sm text-muted-foreground"
        tabIndex={0}
        role="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setExpanded(!expanded);
          }
        }}
      >
        <DropdownCaret open={expanded} />
        <span className="font-medium">Thinking</span>
      </div>
      {expanded && content && (
        <div
          className="prose-sm px-3 pb-3 text-sm text-muted-foreground"
          onClick={(e) => e.stopPropagation()}
        >
          <Markdown content={processed} id={`${messageId}-thinking`} />
        </div>
      )}
    </div>
  );
}
