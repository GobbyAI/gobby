import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import type { StageEntry } from "./StagesTabData";

interface StagesListProps {
  stages: StageEntry[];
  selectedName: string | null;
  busyName: string | null;
  onSelect: (stage: StageEntry) => void;
  onDelete: (stage: StageEntry) => void;
  onRestore: (stage: StageEntry) => void;
}

export function StagesList({
  stages,
  selectedName,
  busyName,
  onSelect,
  onDelete,
  onRestore,
}: StagesListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Stages">
      {stages.map((stage) => {
        const selected = stage.name === selectedName;
        const busy = stage.name === busyName;
        const menuItems: QuickMenuItem[] = stage.deleted_at
          ? [{ label: "Restore", disabled: busy, onSelect: () => onRestore(stage) }]
          : [
              {
                label: "Delete",
                destructive: true,
                disabled: busy,
                onSelect: () => onDelete(stage),
              },
            ];

        return (
          <div
            key={stage.name}
            role="listitem"
            aria-label={`${stage.display_label} stage`}
            className={cn(
              "flex min-h-11 items-center border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${stage.display_label}`}
              onClick={() => onSelect(stage)}
            >
              <ActivityRowStatusDot
                kind={stage.requires_human ? "warning" : "active"}
                label={stage.requires_human ? "Human review required" : "Automated stage"}
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="activity-row-title">{stage.display_label}</span>
                <span className="activity-row-meta truncate">{stage.description}</span>
              </span>
              <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">
                {stage.category}
              </span>
              <span className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground">
                {stage.name}
              </span>
            </button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${stage.display_label}`}
                triggerLabel={`Open actions for ${stage.display_label}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
