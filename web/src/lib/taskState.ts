import {
  currentStage as selectCurrentStage,
  type LifecycleTask,
  type StageState5,
  type StageStateView,
} from './stageActions'
import {
  ActivityGlyph,
  CheckGlyph,
  CircleGlyph,
  DashGlyph,
  EyeGlyph,
  LockGlyph,
  type StatusGlyph,
  type StatusKind,
} from '../components/activity/ActivityRowStatusDot'

export type TaskDisplayState =
  | 'ready'
  | 'in_progress'
  | 'needs_review'
  | 'blocked'
  | 'review_approved'
  | 'closed'

export interface CanonicalTaskState {
  owner_session_id: string | null
  current_stage: StageStateView | null
  is_claimed: boolean
  is_closed: boolean
  is_escalated: boolean
  is_blocked: boolean
  is_merge_ready: boolean
  closed_at: string | null
  closed_reason: string | null
  closed_in_session_id: string | null
  closed_commit_sha: string | null
  escalated_at: string | null
  escalation_reason: string | null
}

export interface TaskCompatProjection {
  assignee?: string | null
}

export interface TaskStateLike {
  assignee?: string | null
  claimed_by_session_id?: string | null
  closed_at?: string | null
  closed_reason?: string | null
  closed_in_session_id?: string | null
  closed_commit_sha?: string | null
  escalated_at?: string | null
  escalation_reason?: string | null
  is_blocked?: boolean | null
  current_stage?: StageStateView | null
  stages?: StageStateView[] | null
  state?: Partial<CanonicalTaskState> | null
  compat?: TaskCompatProjection | null
}

export interface TaskStateBadgeToken {
  key: string
  label: string
  color: string
  background: string
}

export const TASK_STATE_ORDER: TaskDisplayState[] = [
  'ready',
  'in_progress',
  'needs_review',
  'blocked',
  'review_approved',
  'closed',
]

export const TASK_STATE_LABELS: Record<TaskDisplayState | StageState5, string> = {
  ready: 'Ready',
  in_progress: 'In Progress',
  needs_review: 'Needs Review',
  blocked: 'Blocked',
  review_approved: 'Review Approved',
  done: 'Done',
  closed: 'Closed',
}

export const TASK_STATE_COLORS: Record<TaskDisplayState, string> = {
  ready: 'var(--color-info)',
  in_progress: 'var(--color-warning-foreground)',
  needs_review: 'var(--color-info)',
  blocked: 'var(--color-error)',
  review_approved: 'var(--color-review)',
  closed: 'var(--text-muted)',
}

export const TASK_STATE_KIND: Record<TaskDisplayState, StatusKind> = {
  ready: 'info',
  in_progress: 'warning',
  needs_review: 'info',
  blocked: 'error',
  review_approved: 'success',
  closed: 'disabled',
}

// Honest per-state shapes. TASK_STATE_KIND still drives color/lightness (do
// not remap StatusKind — other surfaces depend on its grayscale ranking);
// this map only corrects the icon so it stops lying:
//   in_progress -> activity pulse (being worked, not an alarm)
//   blocked     -> lock/hold (waiting on a dep, not a failure)
//   ready / needs_review -> distinct neutral shapes (open dot vs. review eye)
//   review_approved -> check ; closed -> muted dash
export const TASK_STATE_GLYPH: Record<TaskDisplayState, StatusGlyph> = {
  ready: CircleGlyph,
  in_progress: ActivityGlyph,
  needs_review: EyeGlyph,
  blocked: LockGlyph,
  review_approved: CheckGlyph,
  closed: DashGlyph,
}

export const TASK_STATE_BG: Record<TaskDisplayState, string> = {
  ready: 'var(--color-info-soft)',
  in_progress: 'var(--color-warning-soft)',
  needs_review: 'var(--color-info-soft)',
  blocked: 'var(--color-error-soft)',
  review_approved: 'var(--color-review-soft)',
  closed: 'color-mix(in srgb, var(--text-muted) 15%, transparent)',
}

function deriveCurrentStage(task: TaskStateLike): StageStateView | null {
  const direct = task.state?.current_stage ?? task.current_stage ?? null
  if (direct) return direct

  const stages = task.stages ?? []
  if (stages.length === 0) return null

  return selectCurrentStage({
    id: '',
    title: '',
    task_type: 'task',
    stages,
  } satisfies LifecycleTask)
}

export function getCanonicalTaskState(task: TaskStateLike): CanonicalTaskState {
  const compatAssignee = task.compat?.assignee ?? task.assignee ?? null
  const ownerSessionId =
    task.state?.owner_session_id ??
    task.claimed_by_session_id ??
    compatAssignee ??
    null
  const current = deriveCurrentStage(task)
  const currentState = current?.state ?? null
  const isClosed =
    (task.state?.is_closed ?? Boolean(task.closed_at)) || currentState === 'done'
  const isEscalated =
    !isClosed &&
    (task.state?.is_escalated ?? Boolean(task.escalated_at))
  const isBlocked = !isClosed && (task.state?.is_blocked ?? task.is_blocked ?? isEscalated)
  const isMergeReady =
    !isClosed &&
    !isEscalated &&
    (task.state?.is_merge_ready ?? currentState === 'review_approved')

  return {
    owner_session_id: ownerSessionId,
    current_stage: current,
    is_claimed: task.state?.is_claimed ?? (
      Boolean(ownerSessionId)
    ),
    is_closed: Boolean(isClosed),
    is_escalated: Boolean(isEscalated),
    is_blocked: Boolean(isBlocked),
    is_merge_ready: Boolean(isMergeReady),
    closed_at: task.state?.closed_at ?? task.closed_at ?? null,
    closed_reason: task.state?.closed_reason ?? task.closed_reason ?? null,
    closed_in_session_id: task.state?.closed_in_session_id ?? task.closed_in_session_id ?? null,
    closed_commit_sha: task.state?.closed_commit_sha ?? task.closed_commit_sha ?? null,
    escalated_at: task.state?.escalated_at ?? task.escalated_at ?? null,
    escalation_reason: task.state?.escalation_reason ?? task.escalation_reason ?? null,
  }
}

export function getTaskDisplayState(task: TaskStateLike): TaskDisplayState {
  const state = getCanonicalTaskState(task)
  if (state.is_closed) return 'closed'
  if (state.is_blocked || state.is_escalated) return 'blocked'
  if (state.is_merge_ready || state.current_stage?.state === 'review_approved') {
    return 'review_approved'
  }
  if (state.current_stage?.state === 'needs_review') return 'needs_review'
  if (state.current_stage?.state === 'in_progress' || state.is_claimed) return 'in_progress'

  return 'ready'
}

export function isTaskClosed(task: TaskStateLike): boolean {
  return getTaskDisplayState(task) === 'closed'
}

export function getTaskStateLabel(task: TaskStateLike): string {
  const displayState = getTaskDisplayState(task)
  const state = getCanonicalTaskState(task)
  const stage = state.current_stage

  if (displayState === 'closed' || displayState === 'blocked' || !stage) {
    return TASK_STATE_LABELS[displayState]
  }

  return `${stage.display_name}: ${TASK_STATE_LABELS[stage.state]}`
}

export function getTaskStateSummary(task: TaskStateLike): string {
  const state = getCanonicalTaskState(task)
  const parts = [getTaskStateLabel(task)]

  if (!state.is_closed && state.is_escalated) {
    parts.push('Escalated')
  }
  if (!state.is_closed && state.is_claimed) {
    parts.push('Claimed')
  }

  return [...new Set(parts)].join(' · ')
}

export function getTaskStateTokens(task: TaskStateLike): TaskStateBadgeToken[] {
  const displayState = getTaskDisplayState(task)
  const state = getCanonicalTaskState(task)
  const tokens: TaskStateBadgeToken[] = [
    {
      key: displayState,
      label: getTaskStateLabel(task),
      color: TASK_STATE_COLORS[displayState],
      background: TASK_STATE_BG[displayState],
    },
  ]

  if (!state.is_closed && state.is_claimed) {
    tokens.push({
      key: 'claimed',
      label: 'Claimed',
      color: 'var(--text-secondary)',
      background: 'color-mix(in srgb, var(--text-secondary) 14%, transparent)',
    })
  }

  if (!state.is_closed && state.is_escalated) {
    tokens.push({
      key: 'escalated',
      label: 'Escalated',
      color: 'var(--color-error)',
      background: 'var(--color-error-soft)',
    })
  }

  return tokens
}

export function countTasksByState(tasks: TaskStateLike[]): Record<TaskDisplayState, number> {
  const counts: Record<TaskDisplayState, number> = {
    ready: 0,
    in_progress: 0,
    needs_review: 0,
    blocked: 0,
    review_approved: 0,
    closed: 0,
  }

  for (const task of tasks) {
    counts[getTaskDisplayState(task)] += 1
  }

  return counts
}

export function matchesTaskStateFilter(task: TaskStateLike, filter: string | null): boolean {
  if (!filter) return true

  const displayState = getTaskDisplayState(task)
  switch (filter) {
    case 'recently_done':
    case 'closed':
      return displayState === 'closed'
    case 'in_review':
      return displayState === 'needs_review' || displayState === 'review_approved'
    case 'open':
    case 'ready':
      return displayState === 'ready'
    case 'review':
    case 'needs_review':
      return displayState === 'needs_review'
    case 'merge_ready':
    case 'review_approved':
      return displayState === 'review_approved'
    case 'escalated':
    case 'blocked':
      return displayState === 'blocked'
    case 'in_progress':
      return displayState === 'in_progress'
    default:
      return displayState === filter || getCanonicalTaskState(task).current_stage?.name === filter
  }
}
