import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import type { PipelineDefinition } from "./PipelinesDefsActions";

interface PipelinesDefsListProps {
  definitions: PipelineDefinition[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (definition: PipelineDefinition) => void;
  onRun: (definition: PipelineDefinition) => void;
  onToggle: (definition: PipelineDefinition) => void;
  onDelete: (definition: PipelineDefinition) => void;
}

function stepCount(definition: PipelineDefinition): number {
  try {
    const data = JSON.parse(definition.definition_json);
    return Array.isArray(data.steps) ? data.steps.length : 0;
  } catch {
    return 0;
  }
}

export function PipelinesDefsList({
  definitions,
  selectedId,
  busyId,
  onSelect,
  onRun,
  onToggle,
  onDelete,
}: PipelinesDefsListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Pipeline definitions">
      {definitions.map((definition) => {
        const selected = definition.id === selectedId;
        const busy = definition.id === busyId;
        const menuItems: QuickMenuItem[] = [
          { label: "Run", disabled: busy || !definition.enabled, onSelect: () => onRun(definition) },
          {
            label: definition.enabled ? "Disable" : "Enable",
            disabled: busy,
            onSelect: () => onToggle(definition),
          },
          {
            label: "Delete",
            destructive: true,
            disabled: busy,
            onSelect: () => onDelete(definition),
          },
        ];

        return (
          <div
            key={definition.id}
            role="listitem"
            aria-label={`${definition.name} pipeline definition`}
            className={cn(
              "flex min-h-11 items-center border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${definition.name}`}
              onClick={() => onSelect(definition)}
            >
              <ActivityRowStatusDot
                kind={definition.enabled ? "active" : "disabled"}
                label={definition.enabled ? "Enabled pipeline" : "Disabled pipeline"}
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="activity-row-title">{definition.name}</span>
                <span className="activity-row-meta truncate">
                  {definition.description || "No description"}
                </span>
              </span>
              <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs font-semibold text-muted-foreground">
                PIPELINE
              </span>
              <span className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground">
                {definition.enabled ? "On" : "Off"}
              </span>
              <span className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground">
                {stepCount(definition)} step{stepCount(definition) !== 1 ? "s" : ""}
              </span>
            </button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${definition.name}`}
                triggerLabel={`Open actions for ${definition.name}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
