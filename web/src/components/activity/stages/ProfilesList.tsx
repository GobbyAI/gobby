import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import type { BuildProfile } from "./StagesTabData";
import { profileKey } from "./StagesTabData";

interface ProfilesListProps {
  profiles: BuildProfile[];
  selectedKey: string | null;
  busyKey: string | null;
  onSelect: (profile: BuildProfile) => void;
  onSetDefault: (profile: BuildProfile) => void;
  onToggleEnabled: (profile: BuildProfile) => void;
  onDelete: (profile: BuildProfile) => void;
  onRestore: (profile: BuildProfile) => void;
}

export function ProfilesList({
  profiles,
  selectedKey,
  busyKey,
  onSelect,
  onSetDefault,
  onToggleEnabled,
  onDelete,
  onRestore,
}: ProfilesListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Profiles">
      {profiles.map((profile) => {
        const key = profileKey(profile);
        const selected = key === selectedKey;
        const busy = key === busyKey;
        const menuItems: QuickMenuItem[] = [
          {
            label: "Set as default",
            disabled: busy || profile.name === "default" || Boolean(profile.deleted_at),
            onSelect: () => onSetDefault(profile),
          },
          {
            label: profile.enabled ? "Disable" : "Enable",
            disabled: busy || Boolean(profile.deleted_at),
            onSelect: () => onToggleEnabled(profile),
          },
          { type: "separator" },
          profile.deleted_at
            ? {
                label: "Restore",
                disabled: busy,
                onSelect: () => onRestore(profile),
              }
            : {
                label: "Delete",
                destructive: true,
                disabled: busy,
                onSelect: () => {
                  if (window.confirm(`Delete "${profile.display_label}"?`)) onDelete(profile);
                },
              },
        ];

        return (
          <div
            key={key}
            role="listitem"
            aria-label={`${profile.display_label} profile`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Select ${profile.display_label}`}
              onClick={() => onSelect(profile)}
            >
              <ActivityRowStatusDot
                kind={profile.enabled ? "active" : "disabled"}
                label={profile.enabled ? "Profile enabled" : "Profile disabled"}
                pulse={profile.enabled}
              />
              <span className="activity-row-title">{profile.display_label}</span>
              <Chip>{profile.source}</Chip>
              <Chip>{profile.isolation}</Chip>
              {profile.name === "default" && (
                <Chip tone="accent">default</Chip>
              )}
            </Button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${profile.display_label}`}
                triggerLabel={`Open actions for ${profile.display_label}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
