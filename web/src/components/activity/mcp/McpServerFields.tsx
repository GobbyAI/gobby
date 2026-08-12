import { useCallback } from "react";

import {
  DetailPaneHeader,
  KeyValueField,
  ProjectSelectField,
  SelectField,
  SwitchField,
  TagsField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import type { McpServerDraft } from "./McpTabActions";

export type { McpServerDraft } from "./McpTabActions";

type McpServerFieldsMode = "create" | "edit";

interface McpServerFieldsProps {
  mode: McpServerFieldsMode;
  source: McpServerDraft | null;
  onSave: (draft: McpServerDraft) => Promise<boolean>;
  onDiscard?: () => void;
}

const TRANSPORT_OPTIONS = [
  { value: "http", label: "HTTP" },
  { value: "stdio", label: "stdio" },
  { value: "websocket", label: "WebSocket" },
  { value: "sse", label: "SSE" },
];

const OAUTH_PROVIDER_OPTIONS = [
  { value: "", label: "None" },
  { value: "github", label: "GitHub" },
  { value: "google", label: "Google" },
  { value: "custom", label: "Custom" },
];

function transportUsesUrl(transport: string): boolean {
  return (
    transport === "http" || transport === "websocket" || transport === "sse"
  );
}

export function McpServerFields({
  mode,
  source,
  onSave,
  onDiscard,
}: McpServerFieldsProps) {
  const draftState = useDetailDraft<McpServerDraft>({
    source,
    onSave,
  });
  const draft = draftState.draft;

  const handleDiscard = useCallback(() => {
    draftState.discard();
    onDiscard?.();
  }, [draftState, onDiscard]);

  if (!draft) {
    return (
      <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
        <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
          Select an MCP server.
        </div>
      </div>
    );
  }

  const usesUrl = transportUsesUrl(draft.transport);
  const title =
    mode === "create" ? "New MCP server" : draft.name || "MCP server";

  return (
    <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
      <DetailPaneHeader
        title={title}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={handleDiscard}
      />
      <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-primary)]">
        <div className="flex flex-col gap-4">
          <TextField
            label="Name"
            ariaLabel="Server name"
            value={draft.name}
            disabled={mode === "edit"}
            placeholder="server-name"
            onChange={(value) => draftState.setField("name", value)}
          />
          <TextAreaField
            label="Description"
            ariaLabel="Description"
            value={draft.description}
            rows={3}
            onChange={(value) => draftState.setField("description", value)}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <ProjectSelectField
              label="Project"
              ariaLabel="Project"
              value={draft.project_id}
              placeholder="Select project"
              onChange={(value) => draftState.setField("project_id", value)}
            />
            <SelectField
              label="Transport"
              ariaLabel="Transport"
              value={draft.transport}
              options={TRANSPORT_OPTIONS}
              onChange={(value) => draftState.setField("transport", value)}
            />
          </div>
          {usesUrl ? (
            <TextField
              label="URL"
              ariaLabel="URL"
              value={draft.url}
              placeholder="https://example.com/mcp"
              onChange={(value) => draftState.setField("url", value)}
            />
          ) : (
            <>
              <TextField
                label="Command"
                ariaLabel="Command"
                value={draft.command}
                placeholder="npx"
                onChange={(value) => draftState.setField("command", value)}
              />
              <TagsField
                label="Arguments"
                ariaLabel="Arguments"
                value={draft.args}
                placeholder="Add argument"
                onChange={(value) => draftState.setField("args", value)}
              />
            </>
          )}
          <KeyValueField
            label="Headers"
            ariaLabel="Headers"
            value={draft.headers}
            onChange={(value) => draftState.setField("headers", value)}
          />
          <KeyValueField
            label="Environment"
            ariaLabel="Environment"
            value={draft.env}
            onChange={(value) => draftState.setField("env", value)}
          />
          <div className="grid gap-4 md:grid-cols-2">
            <SwitchField
              label="Enabled"
              ariaLabel="Enabled"
              value={draft.enabled}
              onChange={(value) => draftState.setField("enabled", value)}
            />
            <SwitchField
              label="Requires OAuth"
              ariaLabel="Requires OAuth"
              value={draft.requires_oauth}
              onChange={(value) => draftState.setField("requires_oauth", value)}
            />
          </div>
          <div className="grid gap-4 md:grid-cols-2">
            <SelectField
              label="OAuth provider"
              ariaLabel="OAuth provider"
              value={draft.oauth_provider}
              options={OAUTH_PROVIDER_OPTIONS}
              onChange={(value) => draftState.setField("oauth_provider", value)}
            />
            <TextField
              label="Connect timeout"
              ariaLabel="Connect timeout"
              value={String(draft.connect_timeout)}
              onChange={(value) => {
                const next = Number.parseFloat(value);
                draftState.setField(
                  "connect_timeout",
                  Number.isFinite(next) ? next : 30,
                );
              }}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
