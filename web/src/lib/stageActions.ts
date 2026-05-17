export type StageState5 = 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done'

export type StageRowState = StageState5

export type ReviewPolicy = 'none' | 'required' | 'optional'

export type StageAdvanceAction =
  | 'start'
  | 'submit_for_review'
  | 'approve_review'
  | 'reject_review'
  | 'complete'

export interface StageStateView {
  name: string
  display_name: string
  category: string
  state: StageState5
  review_policy: ReviewPolicy
  updated_at: string | null
  position?: number | null
  reviewer_agent?: string | null
  work_attempt_count?: number | null
  review_round_count?: number | null
  max_work_attempts?: number | null
  max_review_rounds?: number | null
  artifact_refs?: Record<string, string> | null
  entered_at?: string | null
  entered_by_session_id?: string | null
  completed_at?: string | null
  completed_by_session_id?: string | null
  completed_commit_sha?: string | null
  notes?: string | null
}

export interface LifecycleTask {
  id: string
  title: string
  task_type: string
  stages: StageStateView[]
  state?: { is_blocked?: boolean; escalation_reason?: string | null } | null
  is_blocked?: boolean
  blocked_reason?: string | null
  escalation_reason?: string | null
  ref?: string
  priority?: number | null
}

export function resolveAdvanceAction(
  currentState: StageState5,
  reviewPolicy: ReviewPolicy,
): StageAdvanceAction | null {
  if (currentState === 'ready') return 'start'
  if (currentState === 'in_progress') {
    return reviewPolicy === 'required' ? 'submit_for_review' : 'complete'
  }
  if (currentState === 'needs_review') return 'approve_review'
  if (currentState === 'review_approved') return 'complete'
  if (currentState === 'done') return null
  return null
}

export function taskAtStage(task: LifecycleTask, stageName: string): boolean {
  return task.stages?.some(row => row.name === stageName) ?? false
}

export function taskStateAt(
  task: LifecycleTask,
  stageName: string,
): StageRowState | undefined {
  return task.stages?.find(row => row.name === stageName)?.state
}

export function currentStage(task: LifecycleTask): StageStateView | null {
  if (!task.stages?.length) return null
  const sorted = task.stages
    .map((row, index) => ({ row, index }))
    .sort((a, b) => {
      const aPosition = a.row.position ?? a.index
      const bPosition = b.row.position ?? b.index
      return aPosition - bPosition
    })
  return sorted.find(({ row }) => row.state !== 'done')?.row ?? null
}

function sortedStages(task: LifecycleTask): StageStateView[] {
  return [...(task.stages ?? [])].sort((a, b) => {
    const aPosition = a.position ?? 0
    const bPosition = b.position ?? 0
    return aPosition - bPosition || a.name.localeCompare(b.name)
  })
}

export function terminalStage(task: LifecycleTask): StageStateView | null {
  const stages = sortedStages(task)
  return stages.length ? stages[stages.length - 1] : null
}

export function canonicalBoardStage(task: LifecycleTask): StageStateView | null {
  return currentStage(task) ?? terminalStage(task)
}

type OptimisticTaskState<T extends LifecycleTask> =
  | T['state']
  | (NonNullable<T['state']> & { escalation_reason: null })

export type OptimisticMoveResult<T extends LifecycleTask> =
  | T
  | (Omit<T, 'closed_at' | 'current_stage' | 'escalated_at' | 'stages' | 'state'> & {
    closed_at: null
    current_stage: StageStateView | null
    escalated_at: null
    stages: StageStateView[]
    state: OptimisticTaskState<T>
  })

export function optimisticMoveTaskToStage<T extends LifecycleTask>(
  task: T,
  targetStageName: string,
  updatedAt: string = new Date().toISOString(),
): OptimisticMoveResult<T> {
  const target = task.stages.find(row => row.name === targetStageName)
  if (!target) return task
  const targetPosition = target.position ?? task.stages.indexOf(target)
  const stages = task.stages.map((row, index) => {
    const position = row.position ?? index
    if (position < targetPosition) {
      return {
        ...row,
        state: 'done' as const,
        updated_at: updatedAt,
      }
    }
    return {
      ...row,
      state: 'ready' as const,
      entered_at: null,
      entered_by_session_id: null,
      completed_at: null,
      completed_by_session_id: null,
      completed_commit_sha: null,
      artifact_refs: null,
      notes: null,
      updated_at: updatedAt,
    }
  })
  const current_stage = stages.find(row => row.name === targetStageName) ?? null
  return {
    ...task,
    current_stage,
    state: task.state ? { ...task.state, escalation_reason: null } : task.state,
    closed_at: null,
    escalated_at: null,
    stages,
  }
}
