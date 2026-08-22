import { CodeMirrorEditor } from "../shared/CodeMirrorEditor";
import { Heading } from "../shared/Heading";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { FormField } from "../ui/FormField";
import { Input } from "../ui/Input";
import { TabBar } from "../ui/TabBar";
import { Textarea } from "../ui/Textarea";
import { AgentEditPanel } from "./AgentEditPanel";
import type { AgentEditFormProps, AgentFormData } from "./AgentEditForm.types";
import { AgentProviderSettings } from "./AgentProviderSettings";
import { AgentReadOnlyDetails } from "./AgentReadOnlyDetails";
import { AgentRulesEditor } from "./AgentRulesEditor";
import { AgentSkillsEditor } from "./AgentSkillsEditor";
import { AgentStepsEditor } from "./AgentStepsEditor";
import { AgentToolBlocksEditor } from "./AgentToolBlocksEditor";
import { AgentVariablesEditor } from "./AgentVariablesEditor";

function FormInput({
  label,
  value,
  onChange,
  placeholder,
  required,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <FormField label={`${label}${required ? " *" : ""}`}>
      {({ id, describedBy, invalid }) => (
        <Input
          id={id}
          aria-describedby={describedBy}
          error={invalid}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          required={required}
        />
      )}
    </FormField>
  );
}

function FormTextarea({
  label,
  value,
  onChange,
  placeholder,
  rows = 3,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  return (
    <FormField label={label}>
      {({ id, describedBy, invalid }) => (
        <Textarea
          id={id}
          className="resize-y font-[inherit] leading-[1.4]"
          aria-describedby={describedBy}
          error={invalid}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          rows={rows}
        />
      )}
    </FormField>
  );
}

export function AgentEditForm({
  isOpen,
  readOnly,
  agentItem,
  form,
  onChange,
  onSave,
  onCancel,
  isEditing,
  providerCatalog,
  saveDisabled,
  editingId,
  branches = [],
  isGitProject = true,
  projectId,
  rules,
  onRulesChange,
  ruleSelectors,
  onRuleSelectorsChange,
  variables,
  onVariablesChange,
  sidebarView: sidebarViewProp,
  onViewChange,
  yamlContent,
  onYamlChange,
  onYamlSave,
  pipelines,
  editSkills,
  onSkillsChange,
  steps,
  onStepsChange,
  blockedTools,
  onBlockedToolsChange,
  blockedMcpTools,
  onBlockedMcpToolsChange,
  agentNames = [],
}: AgentEditFormProps) {
  const view = sidebarViewProp ?? "form";
  const set = <K extends keyof AgentFormData>(
    key: K,
    value: AgentFormData[K],
  ) => onChange({ ...form, [key]: value });

  const title = readOnly
    ? agentItem?.definition.name || "Agent"
    : isEditing
      ? "Edit Agent"
      : "Create Agent";

  const headerContent = (
    <>
      {onViewChange && (
        <TabBar
          tabs={[
            { id: "form", label: "Form" },
            { id: "yaml", label: "YAML" },
          ]}
          activeTab={view}
          onTabChange={(tabId) =>
            onViewChange(tabId === "yaml" ? "yaml" : "form")
          }
          ariaLabel="Agent editor view"
          className="mb-0"
        />
      )}
    </>
  );

  const footer = readOnly ? undefined : (
    <>
      <Button className={coarseHitAreaCls} onClick={onCancel} type="button">
        Cancel
      </Button>
      <Button
        variant="primary"
        className={coarseHitAreaCls}
        onClick={view === "yaml" && onYamlSave ? onYamlSave : onSave}
        disabled={saveDisabled}
        type="button"
      >
        {isEditing ? "Save" : "Create"}
      </Button>
    </>
  );

  return (
    <AgentEditPanel
      isOpen={isOpen}
      onClose={onCancel}
      title={title}
      headerContent={headerContent}
      footer={footer}
    >
      {view === "yaml" ? (
        <div className="h-full [&_.codemirror-container]:h-full">
          <CodeMirrorEditor
            content={yamlContent || ""}
            language="yaml"
            readOnly={readOnly}
            onChange={onYamlChange}
            onSave={!readOnly ? onYamlSave : undefined}
          />
        </div>
      ) : readOnly ? (
        agentItem ? (
          <AgentReadOnlyDetails agentItem={agentItem} />
        ) : (
          <div className="px-5 py-4 text-sm text-[var(--text-muted)]">
            Agent details are unavailable.
          </div>
        )
      ) : (
        <>
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <FormInput
              label="Name"
              value={form.name}
              onChange={(value) => set("name", value)}
              placeholder="my-agent"
              required
            />
          </div>

          <AgentProviderSettings
            form={form}
            onChange={onChange}
            providerCatalog={providerCatalog}
            branches={branches}
            isGitProject={isGitProject}
            pipelines={pipelines}
            agentNames={agentNames}
          />

          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading
              level={4}
              className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
            >
              Identity
            </Heading>
            <FormTextarea
              label="Description"
              value={form.description}
              onChange={(value) => set("description", value)}
              placeholder="What this agent does..."
            />
          </div>

          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading
              level={4}
              className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
            >
              Prompts
            </Heading>
            {form.surfaces.includes("persona") && (
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-foreground">
                  Persona prompt
                </span>
                <div className="max-h-100 min-h-50 overflow-hidden rounded-md border border-border [&_.codemirror-container]:h-50">
                  <CodeMirrorEditor
                    content={form.persona_prompt}
                    language="markdown"
                    onChange={(value) => set("persona_prompt", value)}
                  />
                </div>
              </div>
            )}
            {form.surfaces.includes("spawn") && (
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium text-foreground">
                  Agent prompt
                </span>
                <div className="max-h-100 min-h-50 overflow-hidden rounded-md border border-border [&_.codemirror-container]:h-50">
                  <CodeMirrorEditor
                    content={form.agent_prompt}
                    language="markdown"
                    onChange={(value) => set("agent_prompt", value)}
                  />
                </div>
              </div>
            )}
          </div>

          {onRulesChange && rules !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading
                level={4}
                className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
              >
                Rules
              </Heading>
              <AgentRulesEditor
                definitionId={editingId}
                rules={rules}
                onRulesChange={onRulesChange}
                projectId={projectId}
                ruleSelectors={ruleSelectors}
                onRuleSelectorsChange={onRuleSelectorsChange}
              />
            </div>
          )}

          {onSkillsChange && editSkills !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading
                level={4}
                className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
              >
                Skills
              </Heading>
              <AgentSkillsEditor
                skills={editSkills}
                onSkillsChange={onSkillsChange}
                projectId={projectId}
              />
            </div>
          )}

          {onVariablesChange && variables !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading
                level={4}
                className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
              >
                Variables
              </Heading>
              <AgentVariablesEditor
                definitionId={editingId}
                variables={variables}
                onVariablesChange={onVariablesChange}
              />
            </div>
          )}

          {(onBlockedToolsChange || onBlockedMcpToolsChange) && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading
                level={4}
                className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
              >
                Tool Restrictions
              </Heading>
              <AgentToolBlocksEditor
                blockedTools={blockedTools || []}
                onBlockedToolsChange={onBlockedToolsChange}
                blockedMcpTools={blockedMcpTools || []}
                onBlockedMcpToolsChange={onBlockedMcpToolsChange}
              />
            </div>
          )}

          {onStepsChange && steps !== undefined && (
            <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
              <Heading
                level={4}
                className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
              >
                Steps
              </Heading>
              <AgentStepsEditor steps={steps} onChange={onStepsChange} />
            </div>
          )}
        </>
      )}
    </AgentEditPanel>
  );
}
