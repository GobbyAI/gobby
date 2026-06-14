import type { WikiSourceRecord } from "../../../hooks/useWiki";
import { cn } from "../../../lib/utils";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import {
  sourceLabel,
  sourceLinks,
  sourcePath,
} from "./WikiTabData";

interface WikiTabListProps {
  sources: WikiSourceRecord[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (source: WikiSourceRecord) => void;
  onRemove: (source: WikiSourceRecord) => void;
}

export function WikiTabList({
  sources,
  selectedId,
  busyId,
  onSelect,
  onRemove,
}: WikiTabListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Wiki sources">
      {sources.map((source) => {
        const label = sourceLabel(source);
        const path = sourcePath(source) || source.id;
        const selected = source.id === selectedId;
        const busy = source.id === busyId;
        const links = sourceLinks(source);
        const menuItems: QuickMenuItem[] = [
          {
            label: "Remove source",
            destructive: true,
            disabled: busy,
            onSelect: () => onRemove(source),
          },
        ];

        return (
          <div
            key={source.id}
            role="listitem"
            aria-label={`${label} source`}
            className={cn(
              "flex min-h-11 items-stretch border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 flex-col gap-1 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${label}`}
              onClick={() => onSelect(source)}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="truncate text-sm font-medium text-foreground">
                  {label}
                </span>
                {links.length > 0 && (
                  <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">
                    Linked
                  </span>
                )}
              </span>
              <span className="break-all text-xs text-muted-foreground">{path}</span>
            </button>
            <div className="flex items-start px-1 py-2">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${label}`}
                triggerLabel={`Open actions for ${label}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
