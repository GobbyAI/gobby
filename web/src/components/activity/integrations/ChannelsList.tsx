import type { Channel } from "../../../hooks/useIntegrations";
import { cn } from "../../../lib/utils";
import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { IntegrationPlatformIcon } from "./IntegrationPlatformIcon";
import { CHANNEL_DISPLAY_NAMES } from "./channelMetadata";
import {
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
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <button
              type="button"
              className="activity-list-row__body"
              aria-label={`Select ${channel.name}`}
              onClick={() => onSelect(channel)}
            >
              <ActivityRowStatusDot
                kind={statusKindForChannel(channel)}
                label={statusLabelForChannel(channel)}
              />
              <span className="activity-row-title">{channel.name}</span>
              <span className="activity-chip gap-1">
                <IntegrationPlatformIcon type={channel.channel_type} size={12} />
                {CHANNEL_DISPLAY_NAMES[channel.channel_type]}
              </span>
              <span className="activity-chip">
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
