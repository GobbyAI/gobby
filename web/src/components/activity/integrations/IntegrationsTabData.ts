import type { Channel, ChannelStatus, CommsMessage } from "../../../hooks/useIntegrations";
import type { IntegrationSavePayload } from "./IntegrationsTabModel";

export class IntegrationApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "IntegrationApiError";
  }
}

export function isCommunicationsUnavailable(error: unknown): boolean {
  return error instanceof IntegrationApiError && error.status === 503;
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    throw new IntegrationApiError(`Request failed: ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

function normalizeChannels(data: Channel[] | { channels?: Channel[] }): Channel[] {
  return Array.isArray(data) ? data : (data.channels ?? []);
}

function normalizeMessages(data: CommsMessage[] | { messages?: CommsMessage[] }): CommsMessage[] {
  return Array.isArray(data) ? data : (data.messages ?? []);
}

export async function loadIntegrationChannels(): Promise<Channel[]> {
  const data = await fetchJson<Channel[] | { channels?: Channel[] }>("/api/comms/channels");
  return normalizeChannels(data);
}

export async function loadChannelMessages(
  channelId: string,
  limit: number,
): Promise<CommsMessage[]> {
  const params = new URLSearchParams({
    channel_id: channelId,
    limit: String(limit),
  });
  const data = await fetchJson<CommsMessage[] | { messages?: CommsMessage[] }>(
    `/api/comms/messages?${params}`,
  );
  return normalizeMessages(data);
}

export async function fetchIntegrationStatus(channelId: string): Promise<ChannelStatus | null> {
  try {
    return await fetchJson<ChannelStatus>(
      `/api/comms/channels/${encodeURIComponent(channelId)}/status`,
    );
  } catch {
    return null;
  }
}

export async function createIntegrationChannel(payload: IntegrationSavePayload): Promise<Channel> {
  return await fetchJson<Channel>("/api/comms/channels", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      channel_type: payload.channel_type,
      name: payload.name,
      config: payload.config,
      secrets: payload.secrets ?? null,
    }),
  });
}

export async function updateIntegrationChannel(
  channelId: string,
  updates: { config?: Record<string, string | number>; enabled?: boolean },
): Promise<Channel> {
  return await fetchJson<Channel>(`/api/comms/channels/${encodeURIComponent(channelId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(updates),
  });
}

export async function deleteIntegrationChannel(channelId: string): Promise<void> {
  const response = await fetch(`/api/comms/channels/${encodeURIComponent(channelId)}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    throw new IntegrationApiError(`Request failed: ${response.status}`, response.status);
  }
}
