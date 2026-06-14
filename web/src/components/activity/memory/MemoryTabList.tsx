import type { GobbyMemory } from "../../../hooks/useMemory";
import { cn } from "../../../lib/utils";
import { formatRelativeTime } from "../../../utils/formatTime";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { memoryTypeLabel } from "./MemoryTabData";

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
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${memory.content}`}
              onClick={() => onSelect(memory)}
            >
              <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs font-semibold text-muted-foreground">
                {memoryTypeLabel(memory.memory_type)}
              </span>
              <span className="activity-row-title">{previewContent(memory.content)}</span>
              <span className="activity-row-meta">
                {formatRelativeTime(memory.created_at)}
              </span>
            </button>
            <div className="flex items-center px-1">
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
