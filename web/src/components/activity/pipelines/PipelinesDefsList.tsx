import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { coarseHitAreaCls } from "../../ui/controlStyles";
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
    <div
      className="flex flex-col"
      role="list"
      aria-label="Pipeline definitions"
    >
      {definitions.map((definition) => {
        const selected = definition.id === selectedId;
        const busy = definition.id === busyId;
        const steps = stepCount(definition);
        const menuItems: QuickMenuItem[] = [
          {
            label: "Run",
            disabled: busy || !definition.enabled,
            onSelect: () => onRun(definition),
          },
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
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Select ${definition.name}`}
              onClick={() => onSelect(definition)}
            >
              <ActivityRowStatusDot
                kind={definition.enabled ? "active" : "disabled"}
                label={
                  definition.enabled ? "Enabled pipeline" : "Disabled pipeline"
                }
              />
              <span className="activity-row-title">{definition.name}</span>
              <Chip tone="accent" uppercase>
                PIPELINE
              </Chip>
              <Chip tone={definition.enabled ? "accent" : "neutral"}>
                {definition.enabled ? "On" : "Off"}
              </Chip>
              <Chip>
                {steps} step{steps !== 1 ? "s" : ""}
              </Chip>
            </Button>
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
