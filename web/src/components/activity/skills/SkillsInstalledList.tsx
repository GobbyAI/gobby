import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import {
  skillCategory,
  skillSourceKey,
  skillSourceLabel,
  type ActivitySkill,
} from "./SkillsTabData";

interface SkillsInstalledListProps {
  skills: ActivitySkill[];
  selectedId: string | null;
  busyId: string | null;
  projectId?: string | null;
  onSelect: (skill: ActivitySkill) => void;
  onToggle: (skill: ActivitySkill) => void;
  onMoveToProject: (skill: ActivitySkill) => void;
  onMoveToInstalled: (skill: ActivitySkill) => void;
  onExport: (skill: ActivitySkill) => void;
  onRestore: (skill: ActivitySkill) => void;
  onDelete: (skill: ActivitySkill) => void;
}

function statusKind(skill: ActivitySkill) {
  if (skill.deleted_at) return "error";
  return skill.enabled ? "active" : "disabled";
}

function statusLabel(skill: ActivitySkill): string {
  if (skill.deleted_at) return "Deleted skill";
  return skill.enabled ? "Enabled skill" : "Disabled skill";
}

export function SkillsInstalledList({
  skills,
  selectedId,
  busyId,
  projectId,
  onSelect,
  onToggle,
  onMoveToProject,
  onMoveToInstalled,
  onExport,
  onRestore,
  onDelete,
}: SkillsInstalledListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Installed skills">
      {skills.map((skill) => {
        const selected = skill.id === selectedId;
        const busy = skill.id === busyId;
        const source = skillSourceKey(skill);
        const canMoveToProject = Boolean(projectId) && source !== "project" && !skill.deleted_at;
        const canMoveToInstalled = source === "project" && !skill.deleted_at;
        // Soft-deleted skills can only be restored, exported, or purged; deleting
        // again is permanent, so the label says so.
        const menuItems: QuickMenuItem[] = skill.deleted_at
          ? [
              { label: "Restore", disabled: busy, onSelect: () => onRestore(skill) },
              { label: "Export", disabled: busy, onSelect: () => onExport(skill) },
              {
                label: "Delete forever",
                destructive: true,
                disabled: busy,
                onSelect: () => onDelete(skill),
              },
            ]
          : [
              {
                label: skill.enabled ? "Disable" : "Enable",
                disabled: busy,
                onSelect: () => onToggle(skill),
              },
              {
                label: source === "project" ? "Move to installed" : "Move to project",
                disabled: busy || (!canMoveToProject && !canMoveToInstalled),
                onSelect: () =>
                  source === "project" ? onMoveToInstalled(skill) : onMoveToProject(skill),
              },
              { label: "Export", disabled: busy, onSelect: () => onExport(skill) },
              {
                label: "Delete",
                destructive: true,
                disabled: busy,
                onSelect: () => onDelete(skill),
              },
            ];

        return (
          <div
            key={skill.id}
            role="listitem"
            aria-label={`${skill.name} skill`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${skill.name}`}
              onClick={() => onSelect(skill)}
            >
              <ActivityRowStatusDot
                kind={statusKind(skill)}
                label={statusLabel(skill)}
              />
              <span className="activity-row-title">{skill.name}</span>
              <span className="activity-chip">{skillCategory(skill)}</span>
              {source !== "installed" && (
                <span className="activity-chip">{skillSourceLabel(skill)}</span>
              )}
            </button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${skill.name}`}
                triggerLabel={`Open actions for ${skill.name}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
