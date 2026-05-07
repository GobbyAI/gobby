import * as yaml from 'js-yaml'
import type { AgentFormData } from '../agents/AgentEditForm'
import type { WorkflowStep } from '../agents/AgentStepsEditor'
import { AUTO_REASONING_EFFORT } from '../../lib/providerModels'
import type { AgentDefInfo, RuleSelectors } from './AgentsTab.types'

export interface AgentPayloadState {
  form: AgentFormData
  rules: string[]
  ruleSelectors: RuleSelectors | null
  variables: Record<string, unknown>
  skills: string[]
  steps: WorkflowStep[]
  blockedTools: string[]
  blockedMcpTools: string[]
}

export interface AgentEditWorkflowState {
  rules: string[]
  ruleSelectors: RuleSelectors | null
  variables: Record<string, unknown>
  skills: string[]
  steps: WorkflowStep[]
  blockedTools: string[]
  blockedMcpTools: string[]
}

interface YamlBodyDefaults {
  name: string
  surfaces?: string[] | null
  provider: string
  mode: string
  baseBranch: string
  timeout: number
  maxTurns: number
}

function addWorkflowPayload(body: Record<string, unknown>, state: AgentPayloadState) {
  const workflows: Record<string, unknown> = {}
  if (state.form.pipeline) workflows.pipeline = state.form.pipeline
  if (state.rules.length > 0) workflows.rules = state.rules
  if (state.ruleSelectors) workflows.rule_selectors = state.ruleSelectors
  if (Object.keys(state.variables).length > 0) workflows.variables = state.variables
  if (state.skills.length > 0) {
    workflows.skill_selectors = { include: state.skills }
  }
  if (Object.keys(workflows).length > 0) body.workflows = workflows
}

function reasoningEffort(form: AgentFormData) {
  return form.reasoning_effort !== AUTO_REASONING_EFFORT ? form.reasoning_effort : null
}

function reasoningRequired(form: AgentFormData) {
  return form.reasoning_effort !== AUTO_REASONING_EFFORT ? form.reasoning_required : false
}

export function buildCreateAgentBody(state: AgentPayloadState): Record<string, unknown> {
  const { form } = state
  const body: Record<string, unknown> = {
    name: form.name,
    provider: form.provider,
    surfaces: form.surfaces,
    mode: form.mode,
    isolation: form.isolation,
    base_branch: form.base_branch,
    timeout: form.timeout,
    max_turns: form.max_turns,
    reasoning_effort: reasoningEffort(form),
    reasoning_required: reasoningRequired(form),
  }
  if (form.description) body.description = form.description
  if (form.role) body.role = form.role
  if (form.goal) body.goal = form.goal
  if (form.personality) body.personality = form.personality
  if (form.instructions) body.instructions = form.instructions
  if (form.model) body.model = form.model
  if (form.fallback_agent) body.fallback_agent = form.fallback_agent
  addWorkflowPayload(body, state)
  if (state.steps.length > 0) body.steps = state.steps
  body.blocked_tools = state.blockedTools
  body.blocked_mcp_tools = state.blockedMcpTools
  return body
}

export function buildUpdateAgentBody(state: AgentPayloadState): Record<string, unknown> {
  const { form } = state
  const body: Record<string, unknown> = {
    name: form.name,
    description: form.description || null,
    surfaces: form.surfaces,
    role: form.role || null,
    goal: form.goal || null,
    personality: form.personality || null,
    instructions: form.instructions || null,
    provider: form.provider,
    model: form.model || null,
    reasoning_effort: reasoningEffort(form),
    reasoning_required: reasoningRequired(form),
    fallback_agent: form.fallback_agent || null,
    mode: form.mode,
    isolation: form.isolation,
    base_branch: form.base_branch,
    timeout: form.timeout,
    max_turns: form.max_turns,
  }
  addWorkflowPayload(body, state)
  if (state.steps.length > 0) body.steps = state.steps
  body.blocked_tools = state.blockedTools
  body.blocked_mcp_tools = state.blockedMcpTools
  return body
}

export function buildDuplicateAgentBody(
  item: AgentDefInfo,
  newName: string,
): Record<string, unknown> {
  const d = item.definition
  const body: Record<string, unknown> = {
    name: newName,
    provider: d.provider,
    surfaces: d.surfaces || ['spawn'],
    mode: d.mode,
    base_branch: d.base_branch,
    timeout: d.timeout,
    max_turns: d.max_turns,
  }
  if (d.description) body.description = d.description
  if (d.role) body.role = d.role
  if (d.goal) body.goal = d.goal
  if (d.personality) body.personality = d.personality
  if (d.instructions) body.instructions = d.instructions
  if (d.model) body.model = d.model
  if (d.reasoning_effort) body.reasoning_effort = d.reasoning_effort
  if (d.reasoning_required !== undefined && d.reasoning_required !== null) {
    body.reasoning_required = d.reasoning_required
  }
  if (d.fallback_agent) body.fallback_agent = d.fallback_agent
  if (d.isolation) body.isolation = d.isolation
  if (d.workflows) body.workflows = d.workflows
  return body
}

export function agentToFormData(item: AgentDefInfo): AgentFormData {
  const d = item.definition
  return {
    name: d.name,
    description: d.description || '',
    surfaces: d.surfaces || ['spawn'],
    role: d.role || '',
    goal: d.goal || '',
    personality: d.personality || '',
    instructions: d.instructions || '',
    provider: d.provider,
    model: d.model || '',
    reasoning_effort: d.reasoning_effort || AUTO_REASONING_EFFORT,
    reasoning_required: !!d.reasoning_required,
    fallback_agent: d.fallback_agent || '',
    mode: d.mode,
    isolation: d.isolation || 'inherit',
    base_branch: d.base_branch,
    timeout: d.timeout,
    max_turns: d.max_turns,
    pipeline: (d.workflows?.pipeline as string) || '',
  }
}

export function extractAgentEditWorkflowState(item: AgentDefInfo): AgentEditWorkflowState {
  const d = item.definition
  const skillSelectors = d.workflows?.skill_selectors as
    | { include?: string[]; exclude?: string[] }
    | undefined

  let skills: string[]
  if (skillSelectors?.include && skillSelectors.include.length > 0) {
    skills = skillSelectors.include.filter((skill) => skill !== '*')
  } else if (d.skill_profile) {
    skills = Object.keys(d.skill_profile)
  } else {
    skills = []
  }

  return {
    steps: d.steps || [],
    blockedTools: d.blocked_tools || [],
    blockedMcpTools: d.blocked_mcp_tools || [],
    rules: (d.workflows?.rules as string[]) || [],
    ruleSelectors: (d.workflows?.rule_selectors as RuleSelectors | undefined) || null,
    variables: (d.workflows?.variables as Record<string, unknown>) || {},
    skills,
  }
}

export function agentDefToYaml(d: AgentDefInfo['definition']): string {
  const obj: Record<string, unknown> = { name: d.name }
  if (d.description) obj.description = d.description
  if (d.surfaces && d.surfaces.length > 0) obj.surfaces = d.surfaces
  if (d.role) obj.role = d.role
  if (d.goal) obj.goal = d.goal
  if (d.personality) obj.personality = d.personality
  if (d.instructions) obj.instructions = d.instructions
  obj.provider = d.provider
  if (d.model) obj.model = d.model
  if (d.is_local !== undefined && d.is_local !== null) obj.is_local = d.is_local
  if (d.reasoning_effort) obj.reasoning_effort = d.reasoning_effort
  if (d.reasoning_required !== undefined && d.reasoning_required !== null) {
    obj.reasoning_required = d.reasoning_required
  }
  if (d.fallback_agent) obj.fallback_agent = d.fallback_agent
  obj.mode = d.mode
  if (d.isolation) obj.isolation = d.isolation
  obj.base_branch = d.base_branch
  obj.timeout = d.timeout
  obj.max_turns = d.max_turns
  if (d.default_workflow) obj.default_workflow = d.default_workflow
  if (d.workflows) obj.workflows = d.workflows
  if (d.lifecycle_variables && Object.keys(d.lifecycle_variables).length > 0) {
    obj.lifecycle_variables = d.lifecycle_variables
  }
  if (d.default_variables && Object.keys(d.default_variables).length > 0) {
    obj.default_variables = d.default_variables
  }
  if (d.sandbox && Object.keys(d.sandbox).length > 0) obj.sandbox = d.sandbox
  if (d.skill_profile && Object.keys(d.skill_profile).length > 0) {
    obj.skill_profile = d.skill_profile
  }
  if (d.steps && d.steps.length > 0) obj.steps = d.steps
  if (d.step_variables && Object.keys(d.step_variables).length > 0) {
    obj.step_variables = d.step_variables
  }
  if (d.exit_condition) obj.exit_condition = d.exit_condition
  if (d.blocked_tools && d.blocked_tools.length > 0) obj.blocked_tools = d.blocked_tools
  if (d.blocked_mcp_tools && d.blocked_mcp_tools.length > 0) {
    obj.blocked_mcp_tools = d.blocked_mcp_tools
  }
  return yaml.dump(obj, { lineWidth: 120, noRefs: true })
}

export function defaultSidebarYaml(): string {
  return yaml.dump({
    name: '',
    provider: 'inherit',
    mode: 'inherit',
    base_branch: 'inherit',
    timeout: 0,
    max_turns: 0,
  }, { lineWidth: 120, noRefs: true })
}

export function parseYamlObject(content: string): Record<string, unknown> {
  const parsed = yaml.load(content, { schema: yaml.JSON_SCHEMA }) as Record<string, unknown>
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Invalid YAML: expected an object')
  }
  return parsed
}

export function buildYamlDefinitionBody(
  parsed: Record<string, unknown>,
  defaults: YamlBodyDefaults,
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    name: (parsed.name as string) || defaults.name,
    description: parsed.description ?? null,
    sources: parsed.sources ?? null,
    surfaces: parsed.surfaces ?? defaults.surfaces ?? ['spawn'],
    role: parsed.role ?? null,
    goal: parsed.goal ?? null,
    personality: parsed.personality ?? null,
    instructions: parsed.instructions ?? null,
    provider: parsed.provider || defaults.provider,
    model: parsed.model ?? null,
    reasoning_effort: parsed.reasoning_effort ?? null,
    reasoning_required: parsed.reasoning_required ?? false,
    mode: parsed.mode || defaults.mode,
    isolation: parsed.isolation ?? null,
    base_branch: (parsed.base_branch as string) || defaults.baseBranch,
    timeout: parsed.timeout ?? defaults.timeout,
    max_turns: parsed.max_turns ?? defaults.maxTurns,
  }
  if (parsed.workflows) body.workflows = parsed.workflows
  return body
}
