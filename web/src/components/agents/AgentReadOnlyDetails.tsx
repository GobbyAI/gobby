import { Card } from "../ui/Card";
import { Chip } from "../ui/Chip";
import { Heading } from "../shared/Heading";
import type { AgentItemForPanel } from "./AgentEditForm.types";
import { AgentMetaRow as MetaRow } from "./AgentMetaRow";

interface AgentReadOnlyDetailsProps {
  agentItem: AgentItemForPanel;
}

function DefinitionSection({
  title,
  content,
}: {
  title: string;
  content: string;
}) {
  return (
    <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
      <Heading
        level={4}
        className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
      >
        {title}
      </Heading>
      <pre className="m-0 font-sans text-sm leading-relaxed whitespace-pre-wrap text-[var(--text-secondary)]">
        {content}
      </pre>
    </div>
  );
}

export function AgentReadOnlyDetails({ agentItem }: AgentReadOnlyDetailsProps) {
  const definition = agentItem.definition;
  const workflowMetadataKeys = [
    "rules",
    "variables",
    "pipeline",
    "rule_selectors",
  ];
  const workflowEntries = definition.workflows
    ? Object.entries(definition.workflows).filter(
        ([name, value]) =>
          !workflowMetadataKeys.includes(name) &&
          typeof value === "object" &&
          value !== null &&
          !Array.isArray(value),
      )
    : [];
  const includedRuleSelectors =
    definition.workflows?.rule_selectors?.include ?? [];
  const excludedRuleSelectors =
    definition.workflows?.rule_selectors?.exclude ?? [];

  return (
    <>
      <div className="border-b border-border px-5 py-3">
        <MetaRow label="Provider">
          <span>{definition.provider}</span>
        </MetaRow>
        <MetaRow label="Model">
          <span>{definition.model || "(default)"}</span>
        </MetaRow>
        <MetaRow label="Reasoning">
          <span>{definition.reasoning_effort || "Auto"}</span>
        </MetaRow>
        <MetaRow label="Require reasoning">
          <span>{definition.reasoning_required ? "Yes" : "No"}</span>
        </MetaRow>
        {definition.fallback_agent && (
          <MetaRow label="Fallback">
            <span>{definition.fallback_agent}</span>
          </MetaRow>
        )}
        <MetaRow label="Mode">
          <span>{definition.mode}</span>
        </MetaRow>
        <MetaRow label="Isolation">
          <span>{definition.isolation || "none"}</span>
        </MetaRow>
        <MetaRow label="Base branch">
          <span>{definition.base_branch}</span>
        </MetaRow>
        <MetaRow label="Timeout">
          <span>{definition.timeout}s</span>
        </MetaRow>
        {definition.default_workflow && (
          <MetaRow label="Default workflow">
            <span>{definition.default_workflow}</span>
          </MetaRow>
        )}
        {definition.workflows?.pipeline && (
          <MetaRow label="Pipeline">
            <span>{definition.workflows.pipeline}</span>
          </MetaRow>
        )}
      </div>

      {definition.description && (
        <DefinitionSection
          title="Description"
          content={definition.description}
        />
      )}
      {definition.prompts?.persona && (
        <DefinitionSection
          title="Persona prompt"
          content={definition.prompts.persona}
        />
      )}
      {definition.prompts?.agent && (
        <DefinitionSection
          title="Agent prompt"
          content={definition.prompts.agent}
        />
      )}

      {workflowEntries.length > 0 && (
        <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
          <Heading
            level={4}
            className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
          >
            Workflows
          </Heading>
          <div className="flex flex-col gap-1.5">
            {workflowEntries.map(([workflowName, rawWorkflow]) => {
              const workflow = rawWorkflow as {
                type?: string;
                file?: string;
                mode?: string;
                internal?: boolean;
                step_count?: number;
                description?: string;
              };
              return (
                <div
                  key={workflowName}
                  className="flex flex-wrap items-center gap-1.5 text-sm"
                >
                  <span className="font-[inherit] font-semibold text-[var(--text-primary)]">
                    {workflowName}
                  </span>
                  {workflow.type && <Chip>{workflow.type}</Chip>}
                  {workflow.file && <Chip>{workflow.file}</Chip>}
                  {workflow.internal && <Chip>internal</Chip>}
                  {workflow.step_count != null && (
                    <Chip>{workflow.step_count} steps</Chip>
                  )}
                  {workflow.description && (
                    <span className="basis-full text-xs text-[var(--text-muted)]">
                      {workflow.description}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      {definition.workflows?.rules && definition.workflows.rules.length > 0 && (
        <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
          <Heading
            level={4}
            className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
          >
            Rules
          </Heading>
          <div className="flex flex-wrap items-center gap-1.5">
            {definition.workflows.rules.map((name) => (
              <Chip key={name} className="border border-border text-sm">
                {name}
              </Chip>
            ))}
          </div>
        </div>
      )}

      {definition.workflows?.rule_selectors && (
        <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
          <Heading
            level={4}
            className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
          >
            Rule Selectors
          </Heading>
          {includedRuleSelectors.length > 0 && (
            <div>
              <span className="text-xs tracking-[0.3px] text-[var(--text-muted)] uppercase">
                Include
              </span>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {includedRuleSelectors.map((selector) => (
                  <Chip
                    key={selector}
                    tone="info"
                    className="border border-dashed border-[var(--color-info)] text-sm"
                  >
                    {selector}
                  </Chip>
                ))}
              </div>
            </div>
          )}
          {excludedRuleSelectors.length > 0 && (
            <div className="mt-1.5">
              <span className="text-xs tracking-[0.3px] text-[var(--text-muted)] uppercase">
                Exclude
              </span>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {excludedRuleSelectors.map((selector) => (
                  <Chip
                    key={selector}
                    tone="error"
                    className="border border-dashed border-[var(--color-error)] text-sm"
                  >
                    {selector}
                  </Chip>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {definition.workflows?.variables &&
        Object.keys(definition.workflows.variables).length > 0 && (
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading
              level={4}
              className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
            >
              Variables
            </Heading>
            <div className="flex flex-col gap-1">
              {Object.entries(definition.workflows.variables).map(
                ([key, value]) => (
                  <div key={key} className="flex items-center gap-2 text-sm">
                    <code className="min-w-[80px] font-semibold text-[var(--text-primary)]">
                      {key}
                    </code>
                    <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[var(--text-muted)]">
                      {typeof value === "string"
                        ? value
                        : JSON.stringify(value)}
                    </span>
                  </div>
                ),
              )}
            </div>
          </div>
        )}

      {((definition.blocked_tools && definition.blocked_tools.length > 0) ||
        (definition.blocked_mcp_tools &&
          definition.blocked_mcp_tools.length > 0)) && (
        <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
          <Heading
            level={4}
            className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
          >
            Tool Restrictions
          </Heading>
          {definition.blocked_tools && definition.blocked_tools.length > 0 && (
            <div className="flex flex-col gap-1">
              <span className="text-xs tracking-[0.3px] text-[var(--text-muted)] uppercase">
                Blocked Tools
              </span>
              <div className="flex flex-wrap gap-1">
                {definition.blocked_tools.map((tool) => (
                  <Chip key={tool} className="border border-border text-xs">
                    {tool}
                  </Chip>
                ))}
              </div>
            </div>
          )}
          {definition.blocked_mcp_tools &&
            definition.blocked_mcp_tools.length > 0 && (
              <div className="flex flex-col gap-1">
                <span className="text-xs tracking-[0.3px] text-[var(--text-muted)] uppercase">
                  Blocked MCP Tools
                </span>
                <div className="flex flex-wrap gap-1">
                  {definition.blocked_mcp_tools.map((tool) => (
                    <Chip key={tool} className="border border-border text-xs">
                      {tool}
                    </Chip>
                  ))}
                </div>
              </div>
            )}
        </div>
      )}

      {definition.step_workflow?.steps &&
        definition.step_workflow.steps.length > 0 && (
          <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
            <Heading
              level={4}
              className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
            >
              Steps ({definition.step_workflow.steps.length})
            </Heading>
            <div className="flex flex-col gap-1">
              {definition.step_workflow.steps.map((step, index) => (
                <Card
                  key={index}
                  padding="sm"
                  className="flex items-center gap-2 text-sm"
                >
                  <Chip tone="accent">{step.name}</Chip>
                  <span className="text-xs text-[var(--text-muted)]">
                    {step.description || ""}
                    {step.transitions && step.transitions.length > 0
                      ? ` → ${step.transitions.map((transition) => transition.to).join(", ")}`
                      : ""}
                  </span>
                </Card>
              ))}
            </div>
          </div>
        )}

      {definition.sandbox && (
        <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
          <Heading
            level={4}
            className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
          >
            Sandbox
          </Heading>
          <pre className="m-0 overflow-x-auto rounded border border-border bg-[var(--bg-primary)] p-2 font-[inherit] text-xs text-[var(--text-secondary)]">
            {JSON.stringify(definition.sandbox, null, 2)}
          </pre>
        </div>
      )}

      <div className="flex flex-col gap-1.5 border-b border-border px-5 py-3">
        <Heading
          level={4}
          className="mt-0 mb-1 text-sm font-semibold tracking-wider text-[var(--text-muted)] uppercase"
        >
          Source
        </Heading>
        <div className="text-sm text-[var(--text-secondary)] [&_code]:font-[inherit] [&_code]:text-xs [&_code]:break-all [&_code]:text-[var(--text-muted)]">
          {agentItem.source_path ? (
            <code>{agentItem.source_path}</code>
          ) : (
            <span>
              Database ({agentItem.source})
              {agentItem.db_id ? ` — ${agentItem.db_id.slice(0, 8)}` : ""}
            </span>
          )}
        </div>
      </div>
    </>
  );
}
