import type { ReviewPolicy, StageState5, StageStateView } from './stageActions'
import type { CanonicalTaskState, TaskCompatProjection } from './taskState'
import { getCanonicalTaskState, getTaskDisplayState } from './taskState'

const STAGE_STATES: readonly StageState5[] = [
  'ready',
  'in_progress',
  'needs_review',
  'review_approved',
  'done',
]

const REVIEW_POLICIES: readonly ReviewPolicy[] = ['none', 'required', 'optional']

export interface StageRegistryEntry extends StageStateView {
  sequence_order?: number | null
  description?: string | null
  default_agent?: string | null
  requires_human?: boolean | null
  is_terminal?: boolean | null
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
  current_stage?: RawStagePayload | null
  stages?: RawStagePayload[] | null
  state?: Partial<CanonicalTaskState> | null
  compat?: TaskCompatProjection | null
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

function isStageState(value: unknown): value is StageState5 {
  return typeof value === 'string' && STAGE_STATES.includes(value as StageState5)
}

function isReviewPolicy(value: unknown): value is ReviewPolicy {
  return typeof value === 'string' && REVIEW_POLICIES.includes(value as ReviewPolicy)
}

function titleizeStageName(name: string): string {
  return name
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map(part => part[0]?.toUpperCase() + part.slice(1))
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
    requires_human: row.requires_human ?? null,
    is_terminal: row.is_terminal ?? null,
  }
}

export function normalizeStagesRegistryResponse(
  data: StagesRegistryWireResponse | null | undefined,
): StageRegistryEntry[] {
  const rows = data?.stages ?? data?.registry ?? []
  return rows.map(row => normalizeStageRegistryEntry(row))
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
  const stages = (task.stages ?? []).map(stage => normalizeStageRow(stage))
  const currentStage = normalizeCurrentStage(task, stages)
  const projected = {
    ...task,
    ref: task.ref ?? (task.seq_num != null ? `#${task.seq_num}` : task.id),
    title: task.title ?? '',
    priority: task.priority ?? 2,
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
