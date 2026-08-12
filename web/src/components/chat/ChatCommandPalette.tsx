import type { ReactElement, RefObject } from "react";
import type { PaletteItem } from "../../hooks/useColonAutocomplete";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";

interface ChatCommandPaletteProps {
  items: PaletteItem[];
  selectedIndex: number;
  onSelect: (item: PaletteItem) => void;
  paletteRef: RefObject<HTMLDivElement>;
  listboxId: string;
  optionIdPrefix: string;
}

export function ChatCommandPalette({
  items,
  selectedIndex,
  onSelect,
  paletteRef,
  listboxId,
  optionIdPrefix,
}: ChatCommandPaletteProps): ReactElement {
  return (
    <div
      ref={paletteRef}
      id={listboxId}
      className="command-palette absolute right-6 bottom-full left-6 z-10 mb-2 max-h-80 overflow-y-auto rounded-lg border border-border bg-[var(--bg-secondary)] font-sans shadow-[var(--shadow-popover-up,0_-4px_12px_oklch(0%_0_0/0.3))]"
      role="listbox"
      aria-label="Chat commands"
    >
      {items.map((item, index) => (
        <Button
          key={
            item.kind === "command"
              ? item.name
              : `${item.parentCommand}:${item.name}${item.serverName ? `:${item.serverName}` : ""}`
          }
          id={`${optionIdPrefix}-${index}`}
          variant="ghost"
          size="sm"
          dense
          className={cn(
            "block min-h-0 w-full cursor-pointer justify-start rounded-none border-0 px-3 py-2 text-left text-sm font-normal whitespace-normal",
            coarseHitAreaCls,
            index === selectedIndex
              ? "bg-accent/20 text-foreground"
              : "text-muted-foreground hover:bg-muted",
          )}
          type="button"
          role="option"
          aria-selected={index === selectedIndex}
          tabIndex={-1}
          onClick={() => onSelect(item)}
        >
          {item.kind === "command" ? (
            <>
              <span className="font-mono">/{item.name}</span>
              {item.description && (
                <span className="ml-2 text-xs opacity-60">
                  {item.description}
                </span>
              )}
              {item.inputHint && (
                <span className="ml-2 font-mono text-xs opacity-70">
                  {item.inputHint}
                </span>
              )}
            </>
          ) : (
            <>
              <span className="font-mono">{item.name}</span>
              {item.serverName && (
                <span className="ml-1.5 rounded bg-accent/10 px-1 py-0.5 text-[length:var(--text-2xs)] text-accent">
                  {item.serverName}
                </span>
              )}
              {item.description && (
                <span className="ml-2 truncate text-xs opacity-60">
                  {item.description}
                </span>
              )}
            </>
          )}
        </Button>
      ))}
    </div>
  );
}
