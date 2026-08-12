import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import type { AgentDefInfo } from "./AgentsTabData";
import { SOURCE_LABELS, getAgentKey } from "./AgentsTabData";

interface AgentsTabListProps {
  agents: AgentDefInfo[];
  selectedKey: string | null;
  busyKey: string | null;
  onSelect: (agent: AgentDefInfo) => void;
  onToggleEnabled: (agent: AgentDefInfo) => void;
  onDuplicate: (agent: AgentDefInfo) => void;
  onDelete: (agent: AgentDefInfo) => void;
}

export function AgentsTabList({
  agents,
  selectedKey,
  busyKey,
  onSelect,
  onToggleEnabled,
  onDuplicate,
  onDelete,
}: AgentsTabListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Agents">
      {agents.map((agent) => {
        const key = getAgentKey(agent);
        const selected = key === selectedKey;
        const busy = key === busyKey;
        const sourceLabel = SOURCE_LABELS[agent.source] ?? agent.source;
        const menuItems: QuickMenuItem[] = [
          {
            label: agent.enabled ? "Disable" : "Enable",
            disabled: busy || !agent.db_id,
            onSelect: () => onToggleEnabled(agent),
          },
          {
            label: "Duplicate",
            disabled: busy,
            onSelect: () => onDuplicate(agent),
          },
          { type: "separator" },
          {
            label: "Delete",
            destructive: true,
            disabled: busy || !agent.db_id,
            onSelect: () => onDelete(agent),
          },
        ];

        return (
          <div
            key={key}
            role="listitem"
            aria-label={`${agent.definition.name} agent`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Select ${agent.definition.name}`}
              onClick={() => onSelect(agent)}
            >
              <ActivityRowStatusDot
                kind={agent.enabled ? "active" : "disabled"}
                label={agent.enabled ? "Agent enabled" : "Agent disabled"}
                pulse={agent.enabled}
              />
              <span className="activity-row-title">
                {agent.definition.name}
              </span>
              <Chip>{agent.definition.provider}</Chip>
              <Chip>{sourceLabel}</Chip>
            </Button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${agent.definition.name}`}
                triggerLabel={`Open actions for ${agent.definition.name}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
