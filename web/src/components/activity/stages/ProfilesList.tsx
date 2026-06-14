import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { cn } from "../../../lib/utils";
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
                onSelect: () => onDelete(profile),
              },
        ];

        return (
          <div
            key={key}
            role="listitem"
            aria-label={`${profile.display_label} profile`}
            className={cn(
              "flex min-h-11 items-center border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${profile.display_label}`}
              onClick={() => onSelect(profile)}
            >
              <ActivityRowStatusDot
                kind={profile.enabled ? "active" : "disabled"}
                label={profile.enabled ? "Profile enabled" : "Profile disabled"}
                pulse={profile.enabled}
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="activity-row-title">{profile.display_label}</span>
                <span className="activity-row-meta truncate">{profile.description}</span>
              </span>
              <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground">
                {profile.source}
              </span>
              <span className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground">
                {profile.isolation}
              </span>
              {profile.name === "default" && (
                <span className="shrink-0 rounded-md bg-[var(--accent-tint)] px-2 py-1 text-xs font-medium text-accent">
                  default
                </span>
              )}
            </button>
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
