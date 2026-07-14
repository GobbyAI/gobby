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
                onSelect: () => {
                  if (window.confirm(`Delete "${stage.display_label}"?`)) onDelete(stage);
                },
              },
            ];

        return (
          <div
            key={stage.name}
            role="listitem"
            aria-label={`${stage.display_label} stage`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${stage.display_label}`}
              onClick={() => onSelect(stage)}
            >
              <ActivityRowStatusDot
                kind={stage.requires_human ? "warning" : "active"}
                label={stage.requires_human ? "Human review required" : "Automated stage"}
              />
              <span className="activity-row-title">{stage.display_label}</span>
              <span className="activity-chip">{stage.category}</span>
              <span className="activity-chip">{stage.name}</span>
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
