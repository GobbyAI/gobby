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
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${definition.name}`}
              onClick={() => onSelect(definition)}
            >
              <ActivityRowStatusDot
                kind={definition.enabled ? "active" : "disabled"}
                label={definition.enabled ? "Enabled pipeline" : "Disabled pipeline"}
              />
              <span className="activity-row-title">{definition.name}</span>
              <span className="activity-chip">
                PIPELINE
              </span>
              <span className="activity-chip">
                {definition.enabled ? "On" : "Off"}
              </span>
              <span className="activity-chip">
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
