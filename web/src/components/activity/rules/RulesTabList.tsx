import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import type { RuleSummary } from "../../../hooks/useRules";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { coarseHitAreaCls } from "../../ui/controlStyles";

interface RulesTabListProps {
  rules: RuleSummary[];
  selectedName: string | null;
  busyRuleName: string | null;
  onSelect: (rule: RuleSummary) => void;
  onToggle: (rule: RuleSummary) => void;
  onCopy: (rule: RuleSummary) => void;
  onDelete: (rule: RuleSummary) => void;
}

export function RulesTabList({
  rules,
  selectedName,
  busyRuleName,
  onSelect,
  onToggle,
  onCopy,
  onDelete,
}: RulesTabListProps) {
  return (
    <div className="rules-list" role="list" aria-label="Rules">
      {rules.map((rule) => {
        const isSelected = rule.name === selectedName;
        const isBusy = rule.name === busyRuleName;
        const menuItems: QuickMenuItem[] = [
          {
            label: rule.enabled ? "Disable" : "Enable",
            disabled: isBusy,
            onSelect: () => onToggle(rule),
          },
          {
            label: "Copy",
            disabled: isBusy,
            onSelect: () => onCopy(rule),
          },
          { type: "separator" },
          {
            label: "Delete",
            destructive: true,
            disabled: isBusy,
            onSelect: () => onDelete(rule),
          },
        ];

        return (
          <div
            key={rule.id || rule.name}
            role="listitem"
            className={cn("activity-list-row", isSelected && "activity-list-row--selected")}
          >
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Select ${rule.name}`}
              onClick={() => onSelect(rule)}
            >
              <ActivityRowStatusDot
                kind={rule.enabled ? "active" : "disabled"}
                label={rule.enabled ? "Rule enabled" : "Rule disabled"}
              />
              <span className="activity-row-title">{rule.name}</span>
              {rule.event && <Chip className="rules-row__badge">{rule.event}</Chip>}
              {rule.group && <Chip className="rules-row__group">{rule.group}</Chip>}
            </Button>
            <QuickMenu
              items={menuItems}
              menuLabel={`Actions for ${rule.name}`}
              triggerLabel={`Open actions for ${rule.name}`}
              disabled={isBusy}
            />
          </div>
        );
      })}
    </div>
  );
}
