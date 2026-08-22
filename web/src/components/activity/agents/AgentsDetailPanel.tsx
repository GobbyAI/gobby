import { useCallback, useEffect, useMemo } from "react";

import {
  getModelsForProvider,
  getOrderedProviders,
  type ProviderModelEntry,
} from "../../../lib/providerModels";
import { AgentRulesEditor } from "../../agents/AgentRulesEditor";
import { AgentSkillsEditor } from "../../agents/AgentSkillsEditor";
import { AgentStepsEditor } from "../../agents/AgentStepsEditor";
import { AgentToolBlocksEditor } from "../../agents/AgentToolBlocksEditor";
import { AgentVariablesEditor } from "../../agents/AgentVariablesEditor";
import { Chip } from "../../ui/Chip";
import { ActivityPanelEmpty } from "../ActivityPanelEmpty";
import {
  DetailPaneHeader,
  SelectField,
  TagsField,
  TextAreaField,
  TextField,
  useDetailDraft,
} from "../fields";
import type { AgentDefInfo, AgentDraft } from "./AgentsTabData";
import { createAgentDraft, agentToDraft } from "./AgentsTabData";

interface AgentsDetailPanelProps {
  agent: AgentDefInfo | null;
  creating: boolean;
  providerCatalog: ProviderModelEntry[];
  pipelines: { id: string; name: string }[];
  projectId?: string | null;
  onSave: (definitionId: string | null, draft: AgentDraft) => Promise<boolean>;
  onConfirmLeaveChange: (confirmIfDirty: (next: () => void) => void) => void;
  onError: (message: string) => void;
}

const MODE_OPTIONS = ["inherit", "autonomous", "interactive"].map((value) => ({
  value,
  label: value,
}));

const ISOLATION_OPTIONS = ["inherit", "worktree", "clone", "none"].map(
  (value) => ({
    value,
    label: value,
  }),
);

function uniqueOptions(values: string[], current: string) {
  const options = [...values];
  if (current && !options.includes(current)) options.push(current);
  return options.map((value) => ({ value, label: value || "(default)" }));
}

export function AgentsDetailPanel({
  agent,
  creating,
  providerCatalog,
  pipelines,
  projectId,
  onSave,
  onConfirmLeaveChange,
  onError,
}: AgentsDetailPanelProps) {
  const sourceDraft = useMemo(() => {
    if (creating) return createAgentDraft();
    return agent ? agentToDraft(agent) : null;
  }, [agent, creating]);

  const handleSave = useCallback(
    async (draft: AgentDraft) => {
      const name = draft.form.name.trim();
      if (!name) {
        onError("Agent name is required");
        return false;
      }
      try {
        return await onSave(creating ? null : (agent?.db_id ?? null), draft);
      } catch (error) {
        onError(error instanceof Error ? error.message : String(error));
        return false;
      }
    },
    [agent, creating, onError, onSave],
  );

  const draftState = useDetailDraft<AgentDraft>({
    source: sourceDraft,
    onSave: handleSave,
  });

  useEffect(() => {
    onConfirmLeaveChange(draftState.confirmIfDirty);
    return () => onConfirmLeaveChange((next) => next());
  }, [draftState.confirmIfDirty, onConfirmLeaveChange]);

  if (!draftState.draft) {
    return (
      <ActivityPanelEmpty
        heading="Agents"
        body="Select an agent to inspect and edit it."
      />
    );
  }

  const draft = draftState.draft;
  const form = draft.form;
  const providers = getOrderedProviders(
    providerCatalog.map((entry) => entry.provider),
  );
  const providerOptions = uniqueOptions(
    ["inherit", ...providers],
    form.provider,
  );
  const modelOptions = getModelsForProvider(providerCatalog, form.provider);
  const pipelineOptions = [
    { value: "", label: "(none)" },
    ...pipelines.map((pipeline) => ({
      value: pipeline.name,
      label: pipeline.name,
    })),
  ];
  if (
    form.pipeline &&
    !pipelineOptions.some((option) => option.value === form.pipeline)
  ) {
    pipelineOptions.push({ value: form.pipeline, label: form.pipeline });
  }

  const setDraftField = <K extends keyof AgentDraft>(
    key: K,
    value: AgentDraft[K],
  ) => {
    draftState.setField(key, value);
  };
  const setFormField = <K extends keyof AgentDraft["form"]>(
    key: K,
    value: AgentDraft["form"][K],
  ) => {
    setDraftField("form", { ...form, [key]: value });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <DetailPaneHeader
        title={creating ? "New agent" : form.name}
        dirty={draftState.dirty}
        saving={draftState.saving}
        serverChanged={draftState.serverChanged}
        onSave={() => void draftState.save()}
        onDiscard={draftState.discard}
        actions={!creating && agent ? <Chip>{agent.source}</Chip> : null}
      />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
          <TextField
            label="Name"
            ariaLabel="Name"
            value={form.name}
            onChange={(value) => setFormField("name", value)}
          />
          <TextAreaField
            label="Description"
            ariaLabel="Description"
            value={form.description}
            rows={3}
            onChange={(value) => setFormField("description", value)}
          />
          <TagsField
            label="Tags"
            ariaLabel="Tags"
            value={draft.tags}
            onChange={(value) => setDraftField("tags", value)}
            placeholder="tag"
          />
          <SelectField
            label="Provider"
            ariaLabel="Provider"
            value={form.provider}
            options={providerOptions}
            onChange={(value) => {
              setDraftField("form", {
                ...form,
                provider: value,
                model: "",
                reasoning_effort: "auto",
                reasoning_required: false,
              });
            }}
          />
          <TextField
            label="Model"
            ariaLabel="Model"
            value={form.model}
            placeholder={modelOptions[0]?.label ?? "(default)"}
            onChange={(value) => setFormField("model", value)}
          />
          <SelectField
            label="Mode"
            ariaLabel="Mode"
            value={form.mode}
            options={MODE_OPTIONS}
            onChange={(value) => setFormField("mode", value)}
          />
          <SelectField
            label="Isolation"
            ariaLabel="Isolation"
            value={form.isolation}
            options={ISOLATION_OPTIONS}
            onChange={(value) => setFormField("isolation", value)}
          />
          <TextField
            label="Timeout"
            ariaLabel="Timeout"
            value={String(form.timeout)}
            onChange={(value) => setFormField("timeout", Number(value) || 0)}
          />
          <SelectField
            label="Pipeline"
            ariaLabel="Pipeline"
            value={form.pipeline}
            options={pipelineOptions}
            onChange={(value) => setFormField("pipeline", value)}
          />
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4">
          {form.surfaces.includes("persona") && (
            <TextAreaField
              label="Persona prompt"
              ariaLabel="Persona prompt"
              value={form.persona_prompt}
              rows={5}
              onChange={(value) => setFormField("persona_prompt", value)}
            />
          )}
          {form.surfaces.includes("spawn") && (
            <TextAreaField
              label="Agent prompt"
              ariaLabel="Agent prompt"
              value={form.agent_prompt}
              rows={8}
              onChange={(value) => setFormField("agent_prompt", value)}
            />
          )}

          <section className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
            <h3 className="mb-2 text-sm font-medium text-foreground">Skills</h3>
            <AgentSkillsEditor
              skills={draft.skills}
              onSkillsChange={(value) => setDraftField("skills", value)}
              projectId={projectId ?? undefined}
            />
          </section>
          <section className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
            <h3 className="mb-2 text-sm font-medium text-foreground">Rules</h3>
            <AgentRulesEditor
              rules={draft.rules}
              onRulesChange={(value) => setDraftField("rules", value)}
              ruleSelectors={draft.ruleSelectors}
              onRuleSelectorsChange={(value) =>
                setDraftField("ruleSelectors", value)
              }
              projectId={projectId ?? undefined}
            />
          </section>
          <section className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
            <h3 className="mb-2 text-sm font-medium text-foreground">
              Variables
            </h3>
            <AgentVariablesEditor
              variables={draft.variables}
              onVariablesChange={(value) => setDraftField("variables", value)}
            />
          </section>
          <section className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
            <h3 className="mb-2 text-sm font-medium text-foreground">Steps</h3>
            <AgentStepsEditor
              steps={draft.steps}
              onChange={(value) => setDraftField("steps", value)}
            />
          </section>
          <section className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
            <h3 className="mb-2 text-sm font-medium text-foreground">
              Tool blocks
            </h3>
            <AgentToolBlocksEditor
              blockedTools={draft.blockedTools}
              onBlockedToolsChange={(value) =>
                setDraftField("blockedTools", value)
              }
              blockedMcpTools={draft.blockedMcpTools}
              onBlockedMcpToolsChange={(value) =>
                setDraftField("blockedMcpTools", value)
              }
            />
          </section>
        </div>
      </div>
    </div>
  );
}
