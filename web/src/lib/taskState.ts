export type TaskLifecycleStage = 'in_progress' | 'needs_review' | 'review_approved'

export interface CanonicalTaskState {
  owner_session_id: string | null
  lifecycle_stage: TaskLifecycleStage | null
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
  status?: string | null
  assignee?: string | null
}

export interface TaskStateLike {
  status?: string | null
  assignee?: string | null
  claimed_by_session_id?: string | null
  lifecycle_stage?: string | null
  closed_at?: string | null
  closed_reason?: string | null
  closed_in_session_id?: string | null
  closed_commit_sha?: string | null
  escalated_at?: string | null
  escalation_reason?: string | null
  state?: Partial<CanonicalTaskState> | null
  compat?: TaskCompatProjection | null
}

export type TaskBucket =
  | 'ready'
  | 'in_progress'
  | 'review'
  | 'blocked'
  | 'merge_ready'
  | 'closed'

export interface TaskStateBadgeToken {
  key: string
  label: string
  color: string
  background: string
}

export const TASK_BUCKET_ORDER: TaskBucket[] = [
  'ready',
  'in_progress',
  'review',
  'blocked',
  'merge_ready',
  'closed',
]

export const TASK_BUCKET_LABELS: Record<TaskBucket, string> = {
  ready: 'Ready',
  in_progress: 'In Progress',
  review: 'Needs Review',
  blocked: 'Blocked',
  merge_ready: 'Merge Ready',
  closed: 'Closed',
}

export const TASK_BUCKET_COLORS: Record<TaskBucket, string> = {
  ready: '#60a5fa',
  in_progress: '#fb923c',
  review: '#c084fc',
  blocked: '#f87171',
  merge_ready: '#2dd4bf',
  closed: '#9ca3af',
}

export const TASK_BUCKET_BG: Record<TaskBucket, string> = {
  ready: 'rgba(96, 165, 250, 0.15)',
  in_progress: 'rgba(251, 146, 60, 0.15)',
  review: 'rgba(192, 132, 252, 0.15)',
  blocked: 'rgba(248, 113, 113, 0.15)',
  merge_ready: 'rgba(45, 212, 191, 0.15)',
  closed: 'rgba(156, 163, 175, 0.15)',
}

function normalizeLifecycleStage(stage: string | null | undefined): TaskLifecycleStage | null {
  if (!stage || stage === 'open') return null
  if (stage === 'in_progress' || stage === 'needs_review' || stage === 'review_approved') {
    return stage
  }
  return null
}

export function getCanonicalTaskState(task: TaskStateLike): CanonicalTaskState {
  const compatStatus = task.compat?.status ?? task.status ?? null
  const compatAssignee = task.compat?.assignee ?? task.assignee ?? null
  const ownerSessionId =
    task.state?.owner_session_id ??
    task.claimed_by_session_id ??
    compatAssignee ??
    null
  const lifecycleStage = normalizeLifecycleStage(
    task.state?.lifecycle_stage ?? task.lifecycle_stage ?? compatStatus
  )
  const isClosed = (task.state?.is_closed ?? Boolean(task.closed_at)) || compatStatus === 'closed'
  const isEscalated =
    !isClosed &&
    ((task.state?.is_escalated ?? Boolean(task.escalated_at)) || compatStatus === 'escalated')
  const isBlocked = task.state?.is_blocked ?? isEscalated
  const isMergeReady =
    !isClosed &&
    !isEscalated &&
    (task.state?.is_merge_ready ?? lifecycleStage === 'review_approved')

  return {
    owner_session_id: ownerSessionId,
    lifecycle_stage: lifecycleStage,
    is_claimed: task.state?.is_claimed ?? Boolean(ownerSessionId),
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

export function getTaskBucket(task: TaskStateLike): TaskBucket {
  const state = getCanonicalTaskState(task)
  if (state.is_closed) return 'closed'
  if (state.is_blocked) return 'blocked'
  if (state.is_merge_ready) return 'merge_ready'
  if (state.lifecycle_stage === 'needs_review') return 'review'
  if (state.is_claimed || state.lifecycle_stage === 'in_progress') return 'in_progress'
  return 'ready'
}

export function isTaskClosed(task: TaskStateLike): boolean {
  return getTaskBucket(task) === 'closed'
}

export function getTaskBucketLabel(task: TaskStateLike): string {
  return TASK_BUCKET_LABELS[getTaskBucket(task)]
}

export function getTaskStateSummary(task: TaskStateLike): string {
  const bucket = getTaskBucket(task)
  const state = getCanonicalTaskState(task)
  const parts = [TASK_BUCKET_LABELS[bucket]]

  if (!state.is_closed && state.is_escalated) {
    parts.push('Escalated')
  }
  if (!state.is_closed && state.is_claimed) {
    parts.push('Claimed')
  }

  return [...new Set(parts)].join(' · ')
}

export function getTaskStateTokens(task: TaskStateLike): TaskStateBadgeToken[] {
  const bucket = getTaskBucket(task)
  const state = getCanonicalTaskState(task)
  const tokens: TaskStateBadgeToken[] = [
    {
      key: bucket,
      label: TASK_BUCKET_LABELS[bucket],
      color: TASK_BUCKET_COLORS[bucket],
      background: TASK_BUCKET_BG[bucket],
    },
  ]

  if (!state.is_closed && state.is_claimed) {
    tokens.push({
      key: 'claimed',
      label: 'Claimed',
      color: '#94a3b8',
      background: 'rgba(148, 163, 184, 0.14)',
    })
  }

  if (!state.is_closed && state.is_escalated) {
    tokens.push({
      key: 'escalated',
      label: 'Escalated',
      color: '#f87171',
      background: 'rgba(248, 113, 113, 0.15)',
    })
  }

  return tokens
}

export function countTasksByBucket(tasks: TaskStateLike[]): Record<TaskBucket, number> {
  const counts: Record<TaskBucket, number> = {
    ready: 0,
    in_progress: 0,
    review: 0,
    blocked: 0,
    merge_ready: 0,
    closed: 0,
  }

  for (const task of tasks) {
    counts[getTaskBucket(task)] += 1
  }

  return counts
}

export function matchesTaskBucketFilter(task: TaskStateLike, filter: string | null): boolean {
  if (!filter) return true

  const bucket = getTaskBucket(task)
  switch (filter) {
    case 'recently_done':
    case 'closed':
      return bucket === 'closed'
    case 'in_review':
      return bucket === 'review' || bucket === 'merge_ready'
    case 'open':
    case 'ready':
      return bucket === 'ready'
    case 'needs_review':
    case 'review':
      return bucket === 'review'
    case 'review_approved':
    case 'merge_ready':
      return bucket === 'merge_ready'
    case 'escalated':
    case 'blocked':
      return bucket === 'blocked'
    case 'in_progress':
      return bucket === 'in_progress'
    default:
      return bucket === filter
  }
}
