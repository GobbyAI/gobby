import type { Channel } from "../../../hooks/useIntegrations";
import {
  createIntegrationChannel,
  deleteIntegrationChannel,
  updateIntegrationChannel,
} from "./IntegrationsTabData";
import {
  integrationPayloadFromDraft,
  type IntegrationDraft,
  type IntegrationSavePayload,
} from "./IntegrationsTabModel";

export async function saveIntegrationDraft(draft: IntegrationDraft): Promise<Channel> {
  const payload = integrationPayloadFromDraft(draft);
  if (payload.mode === "create") {
    return await createIntegrationChannel(payload);
  }
  if (!payload.id) throw new Error("Missing channel id");
  return await saveExistingIntegration(payload.id, payload);
}

export async function saveExistingIntegration(
  channelId: string,
  payload: IntegrationSavePayload,
): Promise<Channel> {
  return await updateIntegrationChannel(channelId, {
    config: payload.config,
    enabled: payload.enabled,
  });
}

export async function toggleIntegrationChannel(channel: Channel): Promise<Channel> {
  return await updateIntegrationChannel(channel.id, { enabled: !channel.enabled });
}

export async function removeIntegrationChannel(channel: Channel): Promise<void> {
  await deleteIntegrationChannel(channel.id);
}
