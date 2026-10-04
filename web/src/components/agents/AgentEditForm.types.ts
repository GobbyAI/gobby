import type { ProviderModelEntry } from "../../lib/providerModels";
import type { WorkflowStep } from "./AgentStepsEditor";

export interface AgentFormData {
  name: string;
  description: string;
  surfaces: string[];
  persona_prompt: string;
  agent_prompt: string;
  provider: string;
  model: string;
  reasoning_effort: string;
  reasoning_required: boolean;
  isolation: string;
  base_branch: string;
  timeout: number;
  pipeline: string;
  fallback_agent: string;
}

export interface AgentItemForPanel {
  definition: {
    name: string;
    description: string | null;
    surfaces?: string[] | null;
    prompts?: {
      persona?: string | null;
      agent?: string | null;
    } | null;
    provider: string;
    model: string | null;
    reasoning_effort?: string | null;
    reasoning_required?: boolean | null;
    fallback_agent: string | null;
    isolation: string | null;
    base_branch: string;
    timeout: number;
    workflows: {
      pipeline?: string;
      rules?: string[];
      rule_selectors?: { include?: string[]; exclude?: string[] };
      variables?: Record<string, unknown>;
      [key: string]: unknown;
    } | null;
    step_workflow?: {
      steps?: WorkflowStep[] | null;
      variables?: Record<string, unknown> | null;
      exit_condition?: string | null;
    } | null;
    blocked_tools?: string[] | null;
    blocked_mcp_tools?: string[] | null;
  };
  source: string;
  source_path: string | null;
  db_id: string | null;
}

export interface RuleSelectors {
  include: string[];
  exclude: string[];
}

export interface AgentEditFormProps {
  isOpen: boolean;
  readOnly?: boolean;
  agentItem?: AgentItemForPanel | null;
  form: AgentFormData;
  onChange: (form: AgentFormData) => void;
  onSave: () => void;
  onCancel: () => void;
  isEditing: boolean;
  providerCatalog: ProviderModelEntry[];
  saveDisabled?: boolean;
  editingId?: string | null;
  branches?: string[];
  isGitProject?: boolean;
  projectId?: string;
  rules?: string[];
  onRulesChange?: (rules: string[]) => void;
  ruleSelectors?: RuleSelectors | null;
  onRuleSelectorsChange?: (selectors: RuleSelectors) => void;
  variables?: Record<string, unknown>;
  onVariablesChange?: (variables: Record<string, unknown>) => void;
  sidebarView?: "form" | "yaml";
  onViewChange?: (view: "form" | "yaml") => void;
  yamlContent?: string;
  onYamlChange?: (content: string) => void;
  onYamlSave?: () => void;
  pipelines?: { id: string; name: string }[];
  editSkills?: string[];
  onSkillsChange?: (skills: string[]) => void;
  steps?: WorkflowStep[];
  onStepsChange?: (steps: WorkflowStep[]) => void;
  blockedTools?: string[];
  onBlockedToolsChange?: (tools: string[]) => void;
  blockedMcpTools?: string[];
  onBlockedMcpToolsChange?: (tools: string[]) => void;
  agentNames?: string[];
}
