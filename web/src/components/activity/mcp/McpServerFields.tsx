import { useCallback, useState } from "react";

import type { McpTemplate } from "../../../hooks/useMcp";
import { SegmentedControl } from "../../ui/SegmentedControl";
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
import {
  validateMcpTemplateSelection,
  type McpServerDraft,
} from "./McpTabActions";
import { McpTemplatePicker } from "./McpTemplatePicker";

export type { McpServerDraft } from "./McpTabActions";

type McpServerFieldsMode = "create" | "edit";

interface McpServerFieldsProps {
  mode: McpServerFieldsMode;
  source: McpServerDraft | null;
  currentProjectId?: string;
  templates?: readonly McpTemplate[];
  templatesLoading?: boolean;
  templatesError?: string | null;
  fetchTemplates?: (projectId?: string) => Promise<void>;
  onSave: (draft: McpServerDraft) => Promise<boolean>;
  onDiscard?: () => void;
}

type McpCreateMethod = "manual" | "template";

const CREATE_METHOD_OPTIONS = [
  { value: "manual" as const, label: "Manual" },
  { value: "template" as const, label: "From template" },
];

async function noTemplates(): Promise<void> {}

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
  currentProjectId = "",
  templates = [],
  templatesLoading = false,
  templatesError = null,
  fetchTemplates = noTemplates,
  onSave,
  onDiscard,
}: McpServerFieldsProps) {
  const [createMethod, setCreateMethod] = useState<McpCreateMethod>(() =>
    source?.template ? "template" : "manual",
  );
  const [templateErrors, setTemplateErrors] = useState<Record<string, string>>(
    {},
  );
  const validateAndSave = useCallback(
    async (candidate: McpServerDraft) => {
      if (mode !== "create" || createMethod !== "template") {
        setTemplateErrors({});
        return onSave(candidate);
      }
      const selectedTemplate = templates.find(
        (template) => template.name === candidate.template,
      );
      const errors = validateMcpTemplateSelection(selectedTemplate, candidate);
      setTemplateErrors(errors);
      if (Object.keys(errors).length > 0) return false;
      return onSave(candidate);
    },
    [createMethod, mode, onSave, templates],
  );
  const draftState = useDetailDraft<McpServerDraft>({
    source,
    onSave: validateAndSave,
  });
  const draft = draftState.draft;

  const handleDiscard = useCallback(() => {
    draftState.discard();
    setCreateMethod(source?.template ? "template" : "manual");
    setTemplateErrors({});
    onDiscard?.();
  }, [draftState, onDiscard, source?.template]);

  if (!draft) {
    return (
      <div className="flex min-h-0 flex-[1_1_auto] flex-col overflow-hidden bg-[var(--bg-primary)]">
        <div className="min-h-0 flex-[1_1_auto] overflow-auto p-3 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
          Select an MCP server.
        </div>
      </div>
    );
  }

  const handleCreateMethodChange = (next: McpCreateMethod) => {
    setCreateMethod(next);
    setTemplateErrors({});
    draftState.setField("template", "");
    draftState.setField("values", {});
    if (next === "template") {
      const projectId = currentProjectId || draft.project_id;
      draftState.setField("scope", projectId ? "project" : "global");
      draftState.setField("project_id", projectId);
    }
  };

  const handleTemplateChange = (
    changes: Partial<
      Pick<McpServerDraft, "template" | "values" | "scope" | "project_id">
    >,
  ) => {
    setTemplateErrors({});
    if (changes.template !== undefined) {
      draftState.setField("template", changes.template);
    }
    if (changes.values !== undefined) {
      draftState.setField("values", changes.values);
    }
    if (changes.scope !== undefined) {
      draftState.setField("scope", changes.scope);
    }
    if (changes.project_id !== undefined) {
      draftState.setField("project_id", changes.project_id);
    }
  };

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
          {mode === "create" ? (
            <SegmentedControl
              value={createMethod}
              onChange={handleCreateMethodChange}
              options={CREATE_METHOD_OPTIONS}
              ariaLabel="Server setup"
              coarseTouchTarget
              className="self-start"
            />
          ) : null}
          <TextField
            label="Name"
            ariaLabel="Server name"
            value={draft.name}
            disabled={mode === "edit"}
            placeholder="server-name"
            onChange={(value) => draftState.setField("name", value)}
          />
          {mode === "create" && createMethod === "template" ? (
            <McpTemplatePicker
              selection={draft}
              currentProjectId={currentProjectId || draft.project_id}
              templates={templates}
              loading={templatesLoading}
              error={templatesError}
              validationErrors={templateErrors}
              fetchTemplates={fetchTemplates}
              onChange={handleTemplateChange}
            />
          ) : (
            <>
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
                  onChange={(value) => {
                    draftState.setField("project_id", value);
                    draftState.setField("scope", value ? "project" : "global");
                  }}
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
                  onChange={(value) =>
                    draftState.setField("requires_oauth", value)
                  }
                />
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <SelectField
                  label="OAuth provider"
                  ariaLabel="OAuth provider"
                  value={draft.oauth_provider}
                  options={OAUTH_PROVIDER_OPTIONS}
                  onChange={(value) =>
                    draftState.setField("oauth_provider", value)
                  }
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
