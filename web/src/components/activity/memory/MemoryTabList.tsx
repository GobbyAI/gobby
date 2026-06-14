import type { GobbyMemory } from "../../../hooks/useMemory";
import { cn } from "../../../lib/utils";
import { formatRelativeTime } from "../../../utils/formatTime";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { memoryTypeLabel, normalizeMemoryTags } from "./MemoryTabData";

interface MemoryTabListProps {
  memories: GobbyMemory[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (memory: GobbyMemory) => void;
  onCopy: (memory: GobbyMemory) => void;
  onDelete: (memory: GobbyMemory) => void;
}

function previewContent(content: string): string {
  return content.length > 140 ? `${content.slice(0, 140)}...` : content;
}

export function MemoryTabList({
  memories,
  selectedId,
  busyId,
  onSelect,
  onCopy,
  onDelete,
}: MemoryTabListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Memories">
      {memories.map((memory) => {
        const selected = memory.id === selectedId;
        const busy = memory.id === busyId;
        const tags = normalizeMemoryTags(memory.tags);
        const menuItems: QuickMenuItem[] = [
          {
            label: "Copy content",
            disabled: busy,
            onSelect: () => onCopy(memory),
          },
          { type: "separator" },
          {
            label: "Delete",
            destructive: true,
            disabled: busy,
            onSelect: () => onDelete(memory),
          },
        ];

        return (
          <div
            key={memory.id}
            role="listitem"
            aria-label={`${memory.content} memory`}
            className={cn(
              "flex min-h-11 items-stretch border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 flex-col gap-1 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${memory.content}`}
              onClick={() => onSelect(memory)}
            >
              <span className="flex min-w-0 items-center gap-2">
                <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs font-semibold text-muted-foreground">
                  {memoryTypeLabel(memory.memory_type)}
                </span>
                <span className="truncate text-xs text-muted-foreground">
                  Created {formatRelativeTime(memory.created_at)}
                </span>
              </span>
              <span className="line-clamp-2 text-sm leading-snug text-foreground">
                {previewContent(memory.content)}
              </span>
              {tags.length > 0 && (
                <span className="flex flex-wrap gap-1">
                  {tags.slice(0, 3).map((tag) => (
                    <span
                      key={tag}
                      className="rounded-md border border-border bg-[var(--bg-secondary)] px-1.5 py-0.5 text-[10px] text-muted-foreground"
                    >
                      {tag}
                    </span>
                  ))}
                </span>
              )}
            </button>
            <div className="flex items-start px-1 py-2">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${memory.content}`}
                triggerLabel={`Open actions for ${memory.content}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
