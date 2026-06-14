import {
  fetchProviderModelCatalog,
  type ProviderModelEntry,
} from "../../../lib/providerModels";
import type { AgentFormData } from "../../agents/AgentEditForm";
import type { WorkflowStep } from "../../agents/AgentStepsEditor";

export type AgentSourceFilter = "installed" | "project" | "templates";

export interface RuleSelectors {
  include: string[];
  exclude: string[];
}

export interface AgentDefInfo {
  definition: {
    name: string;
    description: string | null;
    surfaces?: string[] | null;
    role: string | null;
    goal: string | null;
    personality: string | null;
    instructions: string | null;
    provider: string;
    model: string | null;
    is_local?: boolean | null;
    reasoning_effort?: string | null;
    reasoning_required?: boolean | null;
    fallback_agent: string | null;
    mode: string;
    isolation: string | null;
    base_branch: string;
    timeout: number;
    max_turns: number;
    default_workflow: string | null;
    sandbox: Record<string, unknown> | null;
    workflows: {
      pipeline?: string;
      rules?: string[];
      variables?: Record<string, unknown>;
      rule_selectors?: RuleSelectors;
      skill_selectors?: { include?: string[]; exclude?: string[] };
      [key: string]: unknown;
    } | null;
    lifecycle_variables: Record<string, unknown>;
    default_variables: Record<string, unknown>;
    steps?: WorkflowStep[] | null;
    step_variables?: Record<string, unknown> | null;
    exit_condition?: string | null;
    blocked_tools?: string[] | null;
    blocked_mcp_tools?: string[] | null;
  };
  source: string;
  source_path: string | null;
  db_id: string | null;
  enabled: boolean;
  overridden_by: string | null;
  deleted_at: string | null;
  tags: string[] | null;
  has_template_update?: boolean;
}

export interface AgentDraft {
  form: AgentFormData;
  enabled: boolean;
  tags: string[];
  rules: string[];
  ruleSelectors: RuleSelectors | null;
  variables: Record<string, unknown>;
  skills: string[];
  steps: WorkflowStep[];
  blockedTools: string[];
  blockedMcpTools: string[];
}

export interface AgentFilterOptions {
  sourceFilter: AgentSourceFilter;
  searchText: string;
}

export const DEFAULT_AGENT_FORM: AgentFormData = {
  name: "",
  description: "",
  surfaces: ["spawn"],
  role: "",
  goal: "",
  personality: "",
  instructions: "",
  provider: "inherit",
  model: "",
  reasoning_effort: "auto",
  reasoning_required: false,
  fallback_agent: "",
  mode: "inherit",
  isolation: "inherit",
  base_branch: "inherit",
  timeout: 0,
  max_turns: 0,
  pipeline: "",
};

export const AGENT_SOURCE_OPTIONS = [
  { value: "installed", label: "Installed" },
  { value: "project", label: "Project" },
  { value: "templates", label: "Templates" },
] as const;

export const SOURCE_LABELS: Record<string, string> = {
  template: "Template",
  installed: "Installed",
  project: "Project",
};

export function getBaseUrl(): string {
  return "";
}

export function getAgentKey(agent: AgentDefInfo): string {
  return agent.db_id ?? `${agent.source}:${agent.definition.name}`;
}

export function createAgentDraft(): AgentDraft {
  return {
    form: { ...DEFAULT_AGENT_FORM },
    enabled: true,
    tags: [],
    rules: [],
    ruleSelectors: null,
    variables: {},
    skills: [],
    steps: [],
    blockedTools: [],
    blockedMcpTools: [],
  };
}

export async function loadAgentDefinitions(
  projectId?: string | null,
): Promise<AgentDefInfo[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  const query = params.toString();
  const response = await fetch(`${getBaseUrl()}/api/agents/definitions${query ? `?${query}` : ""}`);
  const data = await response.json();
  if (response.ok && data.status === "success" && Array.isArray(data.definitions)) {
    return data.definitions as AgentDefInfo[];
  }
  throw new Error("Failed to load agent definitions");
}

export async function loadProviderCatalog(): Promise<ProviderModelEntry[]> {
  return fetchProviderModelCatalog();
}

export async function loadPipelineList(): Promise<{ id: string; name: string }[]> {
  const response = await fetch(`${getBaseUrl()}/api/workflows?workflow_type=pipeline`);
  if (!response.ok) return [];
  const data = await response.json();
  if (!Array.isArray(data?.workflows)) return [];
  return data.workflows
    .filter((workflow: { deleted_at?: string | null }) => !workflow.deleted_at)
    .map((workflow: { id: string; name: string }) => ({
      id: workflow.id,
      name: workflow.name,
    }));
}

export function getAgentNames(definitions: AgentDefInfo[]): string[] {
  return definitions
    .filter((definition) => !definition.deleted_at)
    .map((definition) => definition.definition.name)
    .sort((left, right) => left.localeCompare(right));
}

export function nextCopyName(name: string, existing: Iterable<string>): string {
  const existingNames = new Set(existing);
  const match = name.match(/^(.*)-copy(?:-\d+)?$/);
  const baseName = match?.[1] || name;
  let candidate = `${baseName}-copy`;
  let suffix = 2;
  while (existingNames.has(candidate)) {
    candidate = `${baseName}-copy-${suffix}`;
    suffix += 1;
  }
  return candidate;
}

export function filterAgentDefinitions(
  definitions: AgentDefInfo[],
  options: AgentFilterOptions,
): AgentDefInfo[] {
  const query = options.searchText.trim().toLowerCase();
  return definitions.filter((definition) => {
    if (definition.deleted_at) return false;
    if (options.sourceFilter === "installed") {
      if (definition.source === "template" || definition.source === "project") return false;
    } else if (options.sourceFilter === "project") {
      if (definition.source !== "project") return false;
    } else if (options.sourceFilter === "templates") {
      if (definition.source !== "template") return false;
    }

    if (!query) return true;
    const searchable = [
      definition.definition.name,
      definition.definition.description,
      definition.definition.role,
      definition.definition.provider,
      ...(definition.tags ?? []),
    ]
      .filter((value): value is string => typeof value === "string")
      .join(" ")
      .toLowerCase();
    return searchable.includes(query);
  });
}

export function agentToDraft(agent: AgentDefInfo): AgentDraft {
  const definition = agent.definition;
  const workflows = definition.workflows ?? {};
  const skillSelectors = workflows.skill_selectors;
  return {
    form: {
      name: definition.name,
      description: definition.description ?? "",
      surfaces: definition.surfaces ?? ["spawn"],
      role: definition.role ?? "",
      goal: definition.goal ?? "",
      personality: definition.personality ?? "",
      instructions: definition.instructions ?? "",
      provider: definition.provider,
      model: definition.model ?? "",
      reasoning_effort: definition.reasoning_effort ?? "auto",
      reasoning_required: Boolean(definition.reasoning_required),
      fallback_agent: definition.fallback_agent ?? "",
      mode: definition.mode,
      isolation: definition.isolation ?? "inherit",
      base_branch: definition.base_branch,
      timeout: definition.timeout,
      max_turns: definition.max_turns,
      pipeline: typeof workflows.pipeline === "string" ? workflows.pipeline : "",
    },
    enabled: agent.enabled,
    tags: agent.tags ?? [],
    rules: Array.isArray(workflows.rules) ? workflows.rules : [],
    ruleSelectors: workflows.rule_selectors ?? null,
    variables:
      workflows.variables && typeof workflows.variables === "object"
        ? workflows.variables
        : {},
    skills: Array.isArray(skillSelectors?.include)
      ? skillSelectors.include.filter((skill) => skill !== "*")
      : [],
    steps: definition.steps ?? [],
    blockedTools: definition.blocked_tools ?? [],
    blockedMcpTools: definition.blocked_mcp_tools ?? [],
  };
}
