import type { Channel } from "../../../hooks/useIntegrations";
import { cn } from "../../../lib/utils";
import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { IntegrationPlatformIcon } from "./IntegrationPlatformIcon";
import { CHANNEL_DISPLAY_NAMES } from "./channelMetadata";
import {
  formatRelativeTime,
  statusKindForChannel,
  statusLabelForChannel,
} from "./IntegrationsTabModel";

interface ChannelsListProps {
  channels: Channel[];
  selectedId: string | null;
  busyId: string | null;
  onSelect: (channel: Channel) => void;
  onToggle: (channel: Channel) => void;
  onDelete: (channel: Channel) => void;
}

export function ChannelsList({
  channels,
  selectedId,
  busyId,
  onSelect,
  onToggle,
  onDelete,
}: ChannelsListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Integrations">
      {channels.map((channel) => {
        const selected = channel.id === selectedId;
        const busy = channel.id === busyId;
        const menuItems: QuickMenuItem[] = [
          {
            label: channel.enabled ? "Disable" : "Enable",
            disabled: busy,
            onSelect: () => onToggle(channel),
          },
          {
            label: "Delete",
            destructive: true,
            disabled: busy,
            onSelect: () => onDelete(channel),
          },
        ];

        return (
          <div
            key={channel.id}
            role="listitem"
            aria-label={`${channel.name} integration`}
            className={cn(
              "flex min-h-11 items-center border-b border-border bg-[var(--bg-primary)]",
              selected && "bg-[var(--accent-tint)]",
            )}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-2 px-3 py-2 text-left hover:bg-[var(--surface-tint-subtle)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
              aria-label={`Select ${channel.name}`}
              onClick={() => onSelect(channel)}
            >
              <ActivityRowStatusDot
                kind={statusKindForChannel(channel)}
                label={statusLabelForChannel(channel)}
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="activity-row-title">{channel.name}</span>
                <span className="activity-row-meta truncate">
                  Updated {formatRelativeTime(channel.updated_at)}
                </span>
              </span>
              <span className="hidden shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground sm:inline-flex">
                <IntegrationPlatformIcon type={channel.channel_type} size={12} />
                {CHANNEL_DISPLAY_NAMES[channel.channel_type]}
              </span>
              <span className="shrink-0 rounded-md bg-muted px-2 py-1 text-xs font-semibold text-muted-foreground">
                {channel.enabled ? "On" : "Off"}
              </span>
            </button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${channel.name}`}
                triggerLabel={`Open actions for ${channel.name}`}
                disabled={busy}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
