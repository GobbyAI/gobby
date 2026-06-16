import { useCallback, useEffect, useMemo, useState } from "react";

import type { Channel, ChannelStatus, ChannelType } from "../../../hooks/useIntegrations";
import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { ActivityPanelEmpty, TasksEmptyIcon } from "../ActivityPanelEmpty";
import {
  DetailPaneHeader,
  DetailActionButton,
  KeyValueField,
  SelectField,
  SwitchField,
  TextField,
  useDetailDraft,
} from "../fields";
import { fetchIntegrationStatus } from "./IntegrationsTabData";
import { IntegrationPlatformIcon } from "./IntegrationPlatformIcon";
import { CHANNEL_DISPLAY_NAMES, CHANNEL_TYPE_FIELDS } from "./channelMetadata";
import {
  channelTypeOptions,
  createEmptyDraft,
  draftFromChannel,
  statusKindForChannelStatus,
  supportsWebhook,
  validateIntegrationDraft,
  type IntegrationDraft,
} from "./IntegrationsTabModel";

interface ChannelDetailPanelProps {
  mode: "create" | "edit";
  channel: Channel | null;
  onSave: (draft: IntegrationDraft) => Promise<boolean>;
  onDelete: (channel: Channel) => void;
  onCancelCreate: () => void;
  onShowMessages: () => void;
  onError: (message: string | null) => void;
  onConfirmLeaveChange: (handler: (next: () => void) => void) => void;
}

function statusLabel(status: ChannelStatus | null): string {
  if (!status) return "Status unavailable";
  if (!status.enabled) return "Disabled";
  return status.active ? "Active" : "Inactive";
}

function configValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string" && value.startsWith("$secret:")) return "Configured";
  return String(value);
}

function SecretField({
  label,
  name,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  name: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <input
        type="password"
        className="min-h-11 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2 text-sm text-foreground transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-background"
        aria-label={label}
        name={`integration-secret-${name}`}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function MessagesIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
      <path d="M8 9h8" />
      <path d="M8 13h5" />
    </svg>
  );
}

export function ChannelDetailPanel({
  mode,
  channel,
  onSave,
  onDelete,
  onCancelCreate,
  onShowMessages,
  onError,
  onConfirmLeaveChange,
}: ChannelDetailPanelProps) {
  const source = useMemo(
    () => (mode === "create" ? createEmptyDraft() : channel ? draftFromChannel(channel) : null),
    [channel, mode],
  );
  const [statusState, setStatusState] = useState<{
    channelId: string;
    status: ChannelStatus | null;
  } | null>(null);
  const [copied, setCopied] = useState(false);

  const handleSaveDraft = useCallback(
    async (draft: IntegrationDraft) => {
      const validationMessage = validateIntegrationDraft(draft);
      if (validationMessage) {
        onError(validationMessage);
        return false;
      }
      const saved = await onSave(draft);
      if (saved) onError(null);
      return saved;
    },
    [onError, onSave],
  );

  const draftState = useDetailDraft<IntegrationDraft>({
    source,
    onSave: handleSaveDraft,
  });

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  useEffect(() => {
    let cancelled = false;
    if (mode !== "edit" || !channel) {
      return;
    }
    void fetchIntegrationStatus(channel.id).then((nextStatus) => {
      if (!cancelled) setStatusState({ channelId: channel.id, status: nextStatus });
    });
    return () => {
      cancelled = true;
    };
  }, [channel, mode]);

  const draft = draftState.draft;
  const status =
    statusState && statusState.channelId === channel?.id ? statusState.status : null;
  if (!draft) {
    return (
      <ActivityPanelEmpty
        icon={<TasksEmptyIcon />}
        heading="Integration details"
        body="Select an integration to inspect its status and configuration."
      />
    );
  }

  const fields = CHANNEL_TYPE_FIELDS[draft.channel_type];
  const webhookUrl =
    mode === "edit" && channel && supportsWebhook(channel.channel_type)
      ? `${window.location.origin}/api/comms/webhooks/${channel.name}`
      : null;
  const extraConfig =
    mode === "edit" && channel
      ? Object.fromEntries(
          Object.entries(channel.config_json)
            .filter(([key]) => !fields.some((field) => field.key === key))
            .map(([key, value]) => [key, configValue(value)]),
        )
      : {};

  const setConfigField = (key: string, value: string) => {
    draftState.setField("config", { ...draft.config, [key]: value });
  };
  const setSecretField = (key: string, value: string) => {
    draftState.setField("secrets", { ...draft.secrets, [key]: value });
  };
  const handlePlatformChange = (value: string) => {
    draftState.setField("channel_type", value as ChannelType);
    draftState.setField("config", {});
    draftState.setField("secrets", {});
  };
  const copyWebhookUrl = async () => {
    if (!webhookUrl) return;
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (error) {
      console.error("Failed to copy webhook URL", error);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      <DetailPaneHeader
        title={
          <h2 className="m-0 flex min-w-0 items-center gap-2 text-sm font-medium text-foreground">
            <IntegrationPlatformIcon type={draft.channel_type} size={16} />
            <span className="truncate">
              {mode === "create" ? "New integration" : draft.name}
            </span>
          </h2>
        }
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save(draft)}
        onDiscard={draftState.discard}
        actions={
          <>
            {mode === "create" ? (
              <DetailActionButton
                label="Cancel"
                onClick={() => draftState.confirmIfDirty(onCancelCreate)}
              />
            ) : (
              channel && (
                <>
                  <DetailActionButton
                    label="Messages"
                    icon={<MessagesIcon />}
                    onClick={() => draftState.confirmIfDirty(onShowMessages)}
                  />
                  <DetailActionButton
                    label="Delete"
                    variant="destructive"
                    onClick={() => onDelete(channel)}
                  />
                </>
              )
            )}
          </>
        }
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="grid gap-3">
          {mode === "edit" && (
            <div className="rounded-lg border border-border bg-[var(--bg-secondary)] p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-2 text-sm font-medium text-foreground">
                  <ActivityRowStatusDot
                    kind={statusKindForChannelStatus(status)}
                    label={statusLabel(status)}
                  />
                  {statusLabel(status)}
                </div>
                <span className="activity-chip">
                  {CHANNEL_DISPLAY_NAMES[draft.channel_type]}
                </span>
              </div>
              {status && (
                <div className="mt-3 grid gap-2 text-xs text-muted-foreground">
                  <div className="flex justify-between gap-3">
                    <span>Webhooks</span>
                    <span>{status.supports_webhooks ? "Supported" : "Not supported"}</span>
                  </div>
                  <div className="flex justify-between gap-3">
                    <span>Polling</span>
                    <span>
                      {status.supports_polling
                        ? status.is_polling
                          ? "Active"
                          : "Supported"
                        : "Not supported"}
                    </span>
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="grid gap-3">
            <SelectField
              label="Platform"
              ariaLabel="Platform"
              value={draft.channel_type}
              disabled={mode === "edit"}
              options={channelTypeOptions()}
              onChange={handlePlatformChange}
            />
            <TextField
              label="Name"
              ariaLabel="Name"
              value={draft.name}
              disabled={mode === "edit"}
              placeholder="my-channel"
              onChange={(value) => draftState.setField("name", value)}
            />
            <SwitchField
              label="Enabled"
              ariaLabel="Channel enabled"
              value={draft.enabled}
              onChange={(enabled) => draftState.setField("enabled", enabled)}
            />
          </div>

          {fields.length === 0 ? (
            <div className="rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
              No additional configuration required.
            </div>
          ) : (
            <div className="grid gap-3">
              {fields.map((field) => {
                if (field.secret) {
                  if (mode === "edit") {
                    const current = channel?.config_json[field.key];
                    return (
                      <div key={field.key} className="flex min-h-11 items-center justify-between gap-3 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2">
                        <span className="text-xs font-medium text-muted-foreground">
                          {field.label}
                        </span>
                        <span className="text-sm text-foreground">
                          {configValue(current) || "Configured"}
                        </span>
                      </div>
                    );
                  }
                  return (
                    <SecretField
                      key={field.key}
                      label={field.label}
                      name={field.key}
                      value={draft.secrets[field.key] ?? ""}
                      placeholder={field.placeholder}
                      onChange={(value) => setSecretField(field.key, value)}
                    />
                  );
                }
                return (
                  <TextField
                    key={field.key}
                    label={field.label}
                    ariaLabel={field.label}
                    value={draft.config[field.key] ?? ""}
                    placeholder={field.placeholder}
                    onChange={(value) => setConfigField(field.key, value)}
                  />
                );
              })}
            </div>
          )}

          {Object.keys(extraConfig).length > 0 && (
            <KeyValueField
              label="Additional config"
              ariaLabel="Additional config"
              value={extraConfig}
              disabled
              onChange={() => undefined}
            />
          )}

          {webhookUrl && (
            <div className="rounded-lg border border-border bg-[var(--bg-secondary)] p-3">
              <div className="mb-2 text-xs font-medium text-muted-foreground">Webhook URL</div>
              <div className="flex min-w-0 items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded bg-[var(--code-bg)] px-2 py-1 text-xs text-foreground">
                  {webhookUrl}
                </code>
                <button
                  type="button"
                  className="min-h-8 rounded-md border border-border px-2 text-xs font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11"
                  onClick={() => void copyWebhookUrl()}
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
