import type { ReviewPolicy, StageState5, StageStateView } from './stageActions'
import type { CanonicalTaskState, OwnerSessionRef, TaskCompatProjection } from './taskState'
import { getCanonicalTaskState, getTaskDisplayState } from './taskState'
import { DEFAULT_TASK_PRIORITY } from './taskOptions'

const STAGE_STATES: readonly StageState5[] = [
  'ready',
  'in_progress',
  'needs_review',
  'review_approved',
  'done',
]

const REVIEW_POLICIES: readonly ReviewPolicy[] = ['none', 'required', 'optional']

const RETIRED_STAGE_NAMES = new Set(['test_arch'])
const MAX_TASK_PAYLOAD_EXTRACT_DEPTH = 5

export function isRetiredStageName(name: string | null | undefined): boolean {
  return typeof name === 'string' && RETIRED_STAGE_NAMES.has(name)
}

export interface StageRegistryEntry extends StageStateView {
  sequence_order?: number | null
  description?: string | null
  default_agent?: string | null
  reviewer_agent_selector_json?: string | null
  requires_human?: boolean | null
  is_terminal?: boolean | null
}

export interface ReviewerAgentSelectorRule {
  category?: string
  reviewer_agent: string
}

export interface ReviewerAgentSelector {
  default: string
  rules: ReviewerAgentSelectorRule[]
}

/**
 * Parse a stage registry reviewer selector payload.
 *
 * Backends send this as JSON text so older clients can ignore it. New callers
 * should use this helper instead of parsing `reviewer_agent_selector_json`
 * inline, because malformed rows should degrade to the legacy reviewer field.
 */
export function parseReviewerAgentSelector(
  value: string | null | undefined,
): ReviewerAgentSelector | null {
  if (!value) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(value)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object') return null
  const record = parsed as { default?: unknown; rules?: unknown }
  if (typeof record.default !== 'string' || !record.default.trim()) return null
  const defaultReviewer = record.default.trim()
  const rules = Array.isArray(record.rules)
    ? record.rules.flatMap((rule): ReviewerAgentSelectorRule[] => {
      if (!rule || typeof rule !== 'object') return []
      const item = rule as { category?: unknown; reviewer_agent?: unknown }
      if (typeof item.reviewer_agent !== 'string' || !item.reviewer_agent.trim()) return []
      const category = typeof item.category === 'string' ? item.category.trim() : ''
      return [{
        ...(category
          ? { category }
          : {}),
        reviewer_agent: item.reviewer_agent.trim(),
      }]
    })
    : []
  return { default: defaultReviewer, rules }
}

export interface StagesRegistryWireResponse {
  registry?: RawStagePayload[]
  stages?: RawStagePayload[]
}

export type RawStagePayload = Partial<StageStateView> & {
  stage_name?: string | null
  display_label?: string | null
  position_hint?: number | null
  sequence_order?: number | null
  description?: string | null
  default_agent?: string | null
  reviewer_agent_selector_json?: string | null
  requires_human?: boolean | null
  is_terminal?: boolean | null
}

export type RawTaskPayload = {
  id: string
  ref?: string | null
  title?: string | null
  status?: string | null
  priority?: number | null
  task_type?: string | null
  type?: string | null
  parent_task_id?: string | null
  created_at?: string | null
  updated_at?: string | null
  seq_num?: number | null
  path_cache?: string | null
  assignee?: string | null
  agent_name?: string | null
  sequence_order?: number | null
  start_date?: string | null
  due_date?: string | null
  project_id?: string | null
  closed_at?: string | null
  closed_in_session_id?: string | null
  escalated_at?: string | null
  pre_escalation_status?: string | null
  category?: string | null
  description?: string | null
  labels?: string[] | null
  validation_criteria?: string | null
  owner_session_ref?: OwnerSessionRef | null
  current_stage?: RawStagePayload | null
  stages?: RawStagePayload[] | null
  state?: Partial<CanonicalTaskState> | null
  compat?: TaskCompatProjection | null
  allow_automation?: boolean | null
  yolo?: boolean | null
  isolation?: string | null
  dispatch_failure_count?: number | null
  additional_skills?: string[] | null
  assigned_agent?: string | null
}

export type NormalizedTaskPayload = Omit<RawTaskPayload, 'current_stage' | 'stages' | 'state'> & {
  ref: string
  title: string
  status: string
  priority: number
  task_type: string
  parent_task_id: string | null
  created_at: string
  updated_at: string
  seq_num: number | null
  path_cache: string | null
  assignee: string | null
  agent_name: string | null
  sequence_order: number | null
  start_date: string | null
  due_date: string | null
  project_id: string
  current_stage: StageStateView | null
  stages: StageStateView[]
  state: CanonicalTaskState
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'string'
}

function isOptionalNumber(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'number'
}

function isOptionalBoolean(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'boolean'
}

function isOptionalRecord(value: unknown): boolean {
  return value === undefined || value === null || isRecord(value)
}

function isStringArrayOrNull(value: unknown): boolean {
  return (
    value === undefined ||
    value === null ||
    (Array.isArray(value) && value.every(item => typeof item === 'string'))
  )
}

function isOptionalOwnerSessionRef(value: unknown): boolean {
  if (value === undefined || value === null) return true
  if (!isRecord(value)) return false
  return (
    typeof value.session_id === 'string' &&
    value.session_id.trim().length > 0 &&
    typeof value.ref === 'string' &&
    value.ref.trim().length > 0 &&
    (value.source === undefined || value.source === null || typeof value.source === 'string')
  )
}

type OptionalTaskFieldValidator = (value: unknown) => boolean

const OPTIONAL_TASK_FIELD_VALIDATORS = {
  ref: isOptionalString,
  title: isOptionalString,
  status: isOptionalString,
  task_type: isOptionalString,
  type: isOptionalString,
  parent_task_id: isOptionalString,
  created_at: isOptionalString,
  updated_at: isOptionalString,
  path_cache: isOptionalString,
  assignee: isOptionalString,
  agent_name: isOptionalString,
  start_date: isOptionalString,
  due_date: isOptionalString,
  project_id: isOptionalString,
  closed_at: isOptionalString,
  closed_in_session_id: isOptionalString,
  escalated_at: isOptionalString,
  pre_escalation_status: isOptionalString,
  category: isOptionalString,
  description: isOptionalString,
  validation_criteria: isOptionalString,
  isolation: isOptionalString,
  assigned_agent: isOptionalString,
  priority: isOptionalNumber,
  seq_num: isOptionalNumber,
  sequence_order: isOptionalNumber,
  dispatch_failure_count: isOptionalNumber,
  allow_automation: isOptionalBoolean,
  yolo: isOptionalBoolean,
  current_stage: isOptionalRecord,
  state: isOptionalRecord,
  compat: isOptionalRecord,
  owner_session_ref: isOptionalOwnerSessionRef,
  labels: isStringArrayOrNull,
  additional_skills: isStringArrayOrNull,
} satisfies Partial<Record<keyof RawTaskPayload, OptionalTaskFieldValidator>>

function hasValidOptionalTaskFields(record: Record<string, unknown>): boolean {
  return (
    Object.entries(OPTIONAL_TASK_FIELD_VALIDATORS).every(([field, validate]) =>
      validate(record[field]),
    ) &&
    (record.stages === undefined ||
      record.stages === null ||
      (Array.isArray(record.stages) && record.stages.every(isRecord)))
  )
}

export function isRawTaskPayload(value: unknown): value is RawTaskPayload {
  return isRecord(value) &&
    typeof value.id === 'string' &&
    value.id.length > 0 &&
    hasValidOptionalTaskFields(value)
}

export function extractTaskPayload(
  data: unknown,
  depth = 0,
  seen: WeakSet<object> = new WeakSet(),
): RawTaskPayload | null {
  if (depth >= MAX_TASK_PAYLOAD_EXTRACT_DEPTH) return null
  if (isRawTaskPayload(data)) return data
  if (isRecord(data)) {
    if (seen.has(data)) return null
    seen.add(data)
    if (isRawTaskPayload(data.task)) return data.task
    if (isRecord(data.data)) return extractTaskPayload(data.data, depth + 1, seen)
  }
  return null
}

function isStageState(value: unknown): value is StageState5 {
  return typeof value === 'string' && STAGE_STATES.includes(value as StageState5)
}

function isReviewPolicy(value: unknown): value is ReviewPolicy {
  return typeof value === 'string' && REVIEW_POLICIES.includes(value as ReviewPolicy)
}

const STAGE_NAME_ACRONYMS = new Set([
  'qa',
  'pr',
  'ui',
  'api',
  'cli',
  'mcp',
  'json',
  'xml',
  'http',
  'https',
  'css',
  'html',
  'sql',
  'tui',
  'ide',
  'db',
  'ci',
  'cd',
  'sdk',
  'url',
  'id',
])

function titleizeStageName(name: string): string {
  return name
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map(part => {
      if (STAGE_NAME_ACRONYMS.has(part.toLowerCase())) {
        return part.toUpperCase()
      }
      return (part[0]?.toUpperCase() ?? '') + part.slice(1)
    })
    .join(' ')
}

export function normalizeStageRow(
  row: RawStagePayload | null | undefined,
  fallback?: Partial<StageStateView> | null,
): StageStateView {
  const name = row?.name ?? row?.stage_name ?? fallback?.name ?? ''
  const position =
    row?.position ?? row?.position_hint ?? row?.sequence_order ?? fallback?.position ?? null

  return {
    name,
    display_name:
      row?.display_name ?? row?.display_label ?? fallback?.display_name ?? titleizeStageName(name),
    category: row?.category ?? fallback?.category ?? '',
    state: isStageState(row?.state) ? row.state : (fallback?.state ?? 'ready'),
    review_policy: isReviewPolicy(row?.review_policy)
      ? row.review_policy
      : (fallback?.review_policy ?? 'none'),
    updated_at: row?.updated_at ?? fallback?.updated_at ?? null,
    position,
    reviewer_agent: row?.reviewer_agent ?? fallback?.reviewer_agent ?? null,
    work_attempt_count: row?.work_attempt_count ?? fallback?.work_attempt_count ?? null,
    review_round_count: row?.review_round_count ?? fallback?.review_round_count ?? null,
    max_work_attempts: row?.max_work_attempts ?? fallback?.max_work_attempts ?? null,
    max_review_rounds: row?.max_review_rounds ?? fallback?.max_review_rounds ?? null,
    artifact_refs: row?.artifact_refs ?? fallback?.artifact_refs ?? null,
  }
}

export function normalizeStageRegistryEntry(row: RawStagePayload): StageRegistryEntry {
  const normalized = normalizeStageRow(row)
  const position = normalized.position ?? row.position_hint ?? row.sequence_order ?? null
  return {
    ...normalized,
    position,
    sequence_order: row.sequence_order ?? position,
    description: row.description ?? null,
    default_agent: row.default_agent ?? null,
    reviewer_agent_selector_json: row.reviewer_agent_selector_json ?? null,
    requires_human: row.requires_human ?? null,
    is_terminal: row.is_terminal ?? null,
  }
}

export function normalizeStagesRegistryResponse(
  data: StagesRegistryWireResponse | null | undefined,
): StageRegistryEntry[] {
  const rows = data?.stages ?? data?.registry ?? []
  return rows
    .map(row => normalizeStageRegistryEntry(row))
    .filter(stage => !isRetiredStageName(stage.name))
}

function normalizeCurrentStage(
  task: RawTaskPayload,
  stages: StageStateView[],
): StageStateView | null {
  const rawCurrent = (task.current_stage ?? task.state?.current_stage ?? null) as
    | RawStagePayload
    | null
  if (!rawCurrent) return selectCurrentStageFromRows(stages)

  const currentName = rawCurrent.name ?? rawCurrent.stage_name ?? null
  if (isRetiredStageName(currentName)) return selectCurrentStageFromRows(stages)

  const matchingStage = currentName
    ? stages.find(stage => stage.name === currentName)
    : null
  return normalizeStageRow(rawCurrent, matchingStage)
}

function selectCurrentStageFromRows(stages: StageStateView[]): StageStateView | null {
  if (!stages.length) return null
  return stages
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const aPosition = a.row.position ?? a.index
      const bPosition = b.row.position ?? b.index
      return aPosition - bPosition
    })
    .find(({ row }) => row.state !== 'done')?.row ?? null
}

export function normalizeTaskPayload<T extends RawTaskPayload>(
  task: T,
): T & NormalizedTaskPayload {
  const stages = (task.stages ?? [])
    .map(stage => normalizeStageRow(stage))
    .filter(stage => !isRetiredStageName(stage.name))
  const currentStage = normalizeCurrentStage(task, stages)
  const projected = {
    ...task,
    ref: task.ref ?? (task.seq_num != null ? `#${task.seq_num}` : task.id),
    title: task.title ?? '',
    priority: task.priority ?? DEFAULT_TASK_PRIORITY,
    task_type: task.task_type ?? task.type ?? 'task',
    parent_task_id: task.parent_task_id ?? null,
    created_at: task.created_at ?? '',
    updated_at: task.updated_at ?? '',
    seq_num: task.seq_num ?? null,
    path_cache: task.path_cache ?? null,
    assignee: task.assignee ?? null,
    agent_name: task.agent_name ?? null,
    sequence_order: task.sequence_order ?? null,
    start_date: task.start_date ?? null,
    due_date: task.due_date ?? null,
    project_id: task.project_id ?? '',
    current_stage: currentStage,
    stages,
  }
  const canonical = getCanonicalTaskState({
    ...projected,
    state: {
      ...task.state,
      current_stage: currentStage,
    },
  })
  const status = getTaskDisplayState({ ...projected, state: canonical })

  return {
    ...projected,
    status,
    current_stage: canonical.current_stage,
    state: canonical,
  } as T & NormalizedTaskPayload
}

export function normalizeTaskPayloads<T extends RawTaskPayload>(
  tasks: T[] | undefined,
): Array<T & NormalizedTaskPayload> {
  return (tasks ?? []).map(task => normalizeTaskPayload(task))
}
