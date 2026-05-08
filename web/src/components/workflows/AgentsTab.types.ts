import type { AgentFormData } from '../agents/AgentEditForm'
import type { WorkflowStep } from '../agents/AgentStepsEditor'
import { AUTO_REASONING_EFFORT } from '../../lib/providerModels'

export type AgentSourceFilter = 'installed' | 'project' | 'templates' | 'deleted'

export interface AgentsTabProps {
  searchText: string
  sourceFilter: AgentSourceFilter
  devMode: boolean
  showCreateForm: boolean
  onToggleCreateForm: (show: boolean) => void
  refreshKey?: number
  projectId?: string
  hideGobby?: boolean
  hideInstalled?: boolean
  filterProvider: string
  onProvidersChange: (providers: string[]) => void
  tagFilter?: string | null
  onTagsChange?: (tags: string[]) => void
}

export interface RuleSelectors {
  include: string[]
  exclude: string[]
}

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

export function getBaseUrl(): string {
  return ''
}
