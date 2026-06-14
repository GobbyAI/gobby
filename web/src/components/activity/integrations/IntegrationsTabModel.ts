import type { Channel, ChannelStatus, ChannelType } from "../../../hooks/useIntegrations";
import type { StatusKind } from "../ActivityRowStatusDot";
import {
  CHANNEL_DISPLAY_NAMES,
  CHANNEL_TYPE_FIELDS,
  INTEGRATION_CHANNEL_TYPES,
} from "./channelMetadata";

export type IntegrationStatusFilter = "all" | "enabled" | "disabled";

export interface IntegrationFilters {
  search: string;
  channelType: "all" | ChannelType;
  status: IntegrationStatusFilter;
}

export interface IntegrationDraft {
  id: string | null;
  mode: "create" | "edit";
  name: string;
  channel_type: ChannelType;
  enabled: boolean;
  config: Record<string, string>;
  secrets: Record<string, string>;
}

export interface IntegrationSavePayload {
  id: string | null;
  mode: "create" | "edit";
  channel_type: ChannelType;
  name: string;
  enabled: boolean;
  config: Record<string, string | number>;
  secrets?: Record<string, string>;
}

export function channelTypeOptions() {
  return INTEGRATION_CHANNEL_TYPES.map((type) => ({
    value: type,
    label: CHANNEL_DISPLAY_NAMES[type],
  }));
}

export function filterChannels(channels: Channel[], filters: IntegrationFilters): Channel[] {
  const query = filters.search.trim().toLowerCase();
  return channels.filter((channel) => {
    if (filters.channelType !== "all" && channel.channel_type !== filters.channelType) {
      return false;
    }
    if (filters.status === "enabled" && !channel.enabled) return false;
    if (filters.status === "disabled" && channel.enabled) return false;
    if (!query) return true;
    return (
      channel.name.toLowerCase().includes(query) ||
      CHANNEL_DISPLAY_NAMES[channel.channel_type].toLowerCase().includes(query)
    );
  });
}

export function draftFromChannel(channel: Channel): IntegrationDraft {
  const knownFields = CHANNEL_TYPE_FIELDS[channel.channel_type];
  const config: Record<string, string> = {};
  for (const field of knownFields) {
    if (field.secret) continue;
    const value = channel.config_json[field.key];
    if (value !== null && value !== undefined) {
      config[field.key] = String(value);
    }
  }

  return {
    id: channel.id,
    mode: "edit",
    name: channel.name,
    channel_type: channel.channel_type,
    enabled: channel.enabled,
    config,
    secrets: {},
  };
}

export function createEmptyDraft(): IntegrationDraft {
  return {
    id: null,
    mode: "create",
    name: "",
    channel_type: "slack",
    enabled: true,
    config: {},
    secrets: {},
  };
}

export function integrationPayloadFromDraft(draft: IntegrationDraft): IntegrationSavePayload {
  const fields = CHANNEL_TYPE_FIELDS[draft.channel_type];
  const config: Record<string, string | number> = {};
  const secrets: Record<string, string> = {};

  for (const field of fields) {
    const source = field.secret ? draft.secrets : draft.config;
    const rawValue = source[field.key]?.trim();
    if (!rawValue) continue;
    if (field.secret) {
      secrets[field.key] = rawValue;
    } else {
      config[field.key] = field.type === "number" ? Number(rawValue) : rawValue;
    }
  }

  return {
    id: draft.id,
    mode: draft.mode,
    channel_type: draft.channel_type,
    name: draft.name.trim(),
    enabled: draft.enabled,
    config,
    secrets: Object.keys(secrets).length > 0 ? secrets : undefined,
  };
}

export function validateIntegrationDraft(draft: IntegrationDraft): string | null {
  if (!draft.name.trim()) return "Name is required";
  const fields = CHANNEL_TYPE_FIELDS[draft.channel_type];
  for (const field of fields) {
    if (!field.required) continue;
    if (draft.mode === "edit" && field.secret) continue;
    const source = field.secret ? draft.secrets : draft.config;
    if (!source[field.key]?.trim()) return `${field.label} is required`;
  }
  return null;
}

export function statusKindForChannel(channel: Channel): StatusKind {
  return channel.enabled ? "active" : "disabled";
}

export function statusLabelForChannel(channel: Channel): string {
  return channel.enabled ? "Enabled" : "Disabled";
}

export function statusKindForChannelStatus(status: ChannelStatus | null): StatusKind {
  if (!status) return "info";
  if (!status.enabled) return "disabled";
  return status.active ? "active" : "warning";
}

export function formatRelativeTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diff = Date.now() - date.getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function supportsWebhook(channelType: ChannelType): boolean {
  return ["slack", "telegram", "discord", "teams", "sms"].includes(channelType);
}
