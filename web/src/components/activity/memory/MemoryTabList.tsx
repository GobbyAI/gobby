import type { GobbyMemory } from "../../../hooks/useMemory";
import { cn, previewContent } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import {
  dreamFlagLabel,
  isHiddenMemory,
  memoryDreamFlag,
  memoryScopeLabel,
  memoryTypeLabel,
} from "./MemoryTabData";

interface MemoryTabListProps {
  memories: GobbyMemory[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (memory: GobbyMemory) => void;
  onCopy: (memory: GobbyMemory) => void;
  onDelete: (memory: GobbyMemory) => void;
  onRestore: (memory: GobbyMemory) => void;
}

function EyeOffGlyph() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9.88 9.88a3 3 0 1 0 4.24 4.24" />
      <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
      <path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
      <line x1="2" y1="2" x2="22" y2="22" />
    </svg>
  );
}

function TrashGlyph() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  );
}

function RestoreGlyph() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" />
    </svg>
  );
}

// Text-and-icon badge for a dream-flagged memory. The label and glyph (not hue)
// carry the meaning, so it stays legible in a desaturated screenshot and never
// relies on color alone (impeccable: color is the fourth signal).
function DreamFlagBadge({ memory }: { memory: GobbyMemory }) {
  const flag = memoryDreamFlag(memory);
  const label = dreamFlagLabel(memory);
  if (!label) return null;
  const isDelete = flag === "delete";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-sm border px-1.5 py-0.5 text-2xs font-medium",
        isDelete
          ? "border-destructive/40 bg-destructive/10 text-destructive-foreground"
          : "border-warning/40 bg-warning/10 text-warning-foreground",
      )}
    >
      {isDelete ? <TrashGlyph /> : <EyeOffGlyph />}
      {label}
    </span>
  );
}

export function MemoryTabList({
  memories,
  selectedId,
  busyId,
  onSelect,
  onCopy,
  onDelete,
  onRestore,
}: MemoryTabListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Memories">
      {memories.map((memory) => {
        const selected = memory.id === selectedId;
        const busy = memory.id === busyId;
        const hidden = isHiddenMemory(memory);
        const restoreItem: QuickMenuItem[] = hidden
          ? [
              {
                label: "Restore",
                icon: <RestoreGlyph />,
                disabled: busy,
                onSelect: () => onRestore(memory),
              },
            ]
          : [];
        const menuItems: QuickMenuItem[] = [
          ...restoreItem,
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
            aria-label={
              hidden
                ? `${previewContent(memory.content)} memory, ${dreamFlagLabel(memory)}`
                : `${previewContent(memory.content)} memory`
            }
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
              hidden && "opacity-60",
            )}
          >
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Select ${previewContent(memory.content)}`}
              onClick={() => onSelect(memory)}
            >
              <span className="activity-row-title">{previewContent(memory.content)}</span>
              {hidden && <DreamFlagBadge memory={memory} />}
              <Chip>
                {memoryTypeLabel(memory.memory_type)}
              </Chip>
              <Chip tone={memory.is_global ? "accent" : "neutral"}>
                {memoryScopeLabel(memory)}
              </Chip>
            </Button>
            <div className="flex items-center px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${previewContent(memory.content)}`}
                triggerLabel={`Open actions for ${previewContent(memory.content)}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
