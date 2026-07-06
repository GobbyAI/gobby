import type { AgentDefInfo, AgentDraft } from "./AgentsTabData";
import { agentToDraft, getBaseUrl } from "./AgentsTabData";

function optionalString(value: string): string | null {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function requireNonNegativeNumber(value: number, label: string): number {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`${label} must be a non-negative number`);
  }
  return value;
}

function workflowPayload(draft: AgentDraft): Record<string, unknown> | null {
  const workflows: Record<string, unknown> = {};
  if (draft.form.pipeline) workflows.pipeline = draft.form.pipeline;
  if (draft.rules.length > 0) workflows.rules = draft.rules;
  if (draft.ruleSelectors) workflows.rule_selectors = draft.ruleSelectors;
  if (Object.keys(draft.variables).length > 0) workflows.variables = draft.variables;
  if (draft.skills.length > 0) {
    workflows.skill_selectors = { include: draft.skills };
  }
  return Object.keys(workflows).length > 0 ? workflows : null;
}

export function buildAgentDefinitionBody(
  draft: AgentDraft,
  projectId?: string | null,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: draft.form.name.trim(),
    description: optionalString(draft.form.description),
    surfaces: draft.form.surfaces,
    role: optionalString(draft.form.role),
    goal: optionalString(draft.form.goal),
    personality: optionalString(draft.form.personality),
    instructions: optionalString(draft.form.instructions),
    provider: draft.form.provider,
    model: optionalString(draft.form.model),
    reasoning_effort:
      draft.form.reasoning_effort === "auto" ? null : draft.form.reasoning_effort,
    reasoning_required:
      draft.form.reasoning_effort === "auto" ? false : draft.form.reasoning_required,
    fallback_agent: optionalString(draft.form.fallback_agent),
    mode: draft.form.mode,
    isolation: draft.form.isolation,
    base_branch: draft.form.base_branch,
    timeout: requireNonNegativeNumber(draft.form.timeout, "Timeout"),
    enabled: draft.enabled,
    tags: draft.tags,
    workflows: workflowPayload(draft),
    steps: draft.steps,
    blocked_tools: draft.blockedTools,
    blocked_mcp_tools: draft.blockedMcpTools,
  };
  if (projectId) body.project_id = projectId;
  return body;
}

async function sendJson(url: string, method: string, body?: Record<string, unknown>) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    const detail = await response
      .json()
      .then((data: unknown) =>
        data && typeof data === "object" && "detail" in data
          ? String((data as { detail: unknown }).detail)
          : null,
      )
      .catch(() => null);
    throw new Error(
      detail
        ? `Agent request failed with ${response.status}: ${detail}`
        : `Agent request failed with ${response.status}`,
    );
  }
  return response.json().catch(() => ({}));
}

export async function saveAgentDraft(options: {
  draft: AgentDraft;
  definitionId: string | null;
  projectId?: string | null;
}): Promise<boolean> {
  const body = buildAgentDefinitionBody(options.draft, options.definitionId ? null : options.projectId);
  if (options.definitionId) {
    await sendJson(`${getBaseUrl()}/api/agents/definitions/${options.definitionId}`, "PUT", body);
    return true;
  }
  await sendJson(`${getBaseUrl()}/api/agents/definitions`, "POST", body);
  return true;
}

export async function setAgentEnabled(agent: AgentDefInfo, enabled: boolean): Promise<boolean> {
  if (!agent.db_id) return false;
  const draft = agentToDraft(agent);
  draft.enabled = enabled;
  await sendJson(
    `${getBaseUrl()}/api/agents/definitions/${agent.db_id}`,
    "PUT",
    buildAgentDefinitionBody(draft),
  );
  return true;
}

export async function duplicateAgentDefinition(
  agent: AgentDefInfo,
  newName: string,
  projectId?: string | null,
): Promise<boolean> {
  const draft = agentToDraft(agent);
  draft.form.name = newName;
  await sendJson(
    `${getBaseUrl()}/api/agents/definitions`,
    "POST",
    buildAgentDefinitionBody(draft, projectId),
  );
  return true;
}

export async function deleteAgentDefinition(agent: AgentDefInfo): Promise<boolean> {
  if (!agent.db_id) return false;
  await sendJson(`${getBaseUrl()}/api/agents/definitions/${agent.db_id}`, "DELETE");
  return true;
}
