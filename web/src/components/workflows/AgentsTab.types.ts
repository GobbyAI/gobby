import * as yaml from 'js-yaml'
import type { AgentFormData } from '../agents/AgentEditForm'
import type { WorkflowStep } from '../agents/AgentStepsEditor'
import { AUTO_REASONING_EFFORT } from '../../lib/providerModels'

export interface AgentDefInfo {
  definition: {
    name: string
    description: string | null
    surfaces?: string[] | null
    role: string | null
    goal: string | null
    personality: string | null
    instructions: string | null
    provider: string
    model: string | null
    is_local?: boolean | null
    reasoning_effort?: string | null
    reasoning_required?: boolean | null
    fallback_agent: string | null
    mode: string
    isolation: string | null
    base_branch: string
    timeout: number
    max_turns: number
    default_workflow: string | null
    sandbox: Record<string, unknown> | null
    skill_profile: Record<string, unknown> | null
    workflows: {
      pipeline?: string
      rules?: string[]
      variables?: Record<string, unknown>
      [key: string]: unknown
    } | null
    lifecycle_variables: Record<string, unknown>
    default_variables: Record<string, unknown>
    steps?: WorkflowStep[] | null
    step_variables?: Record<string, unknown> | null
    exit_condition?: string | null
    blocked_tools?: string[] | null
    blocked_mcp_tools?: string[] | null
  }
  source: string
  source_path: string | null
  db_id: string | null
  enabled: boolean
  overridden_by: string | null
  deleted_at: string | null
  tags: string[] | null
  has_template_update?: boolean
}

export const SOURCE_LABELS: Record<string, string> = {
  'template': 'Template',
  'installed': 'Installed',
  'project': 'Project',
}

export const DEFAULT_FORM: AgentFormData = {
  name: '',
  description: '',
  surfaces: ['spawn'],
  role: '',
  goal: '',
  personality: '',
  instructions: '',
  provider: 'inherit',
  model: '',
  reasoning_effort: AUTO_REASONING_EFFORT,
  reasoning_required: false,
  fallback_agent: '',
  mode: 'inherit',
  isolation: 'inherit',
  base_branch: 'inherit',
  timeout: 0,
  max_turns: 0,
  pipeline: '',
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

export function getBaseUrl(): string {
  return ''
}
