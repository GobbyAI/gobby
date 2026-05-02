export type StageState5 = 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done'

export type StageRowState = StageState5

export type ReviewPolicy = 'none' | 'required' | 'optional'

export type StageAdvanceAction = 'start' | 'submit_for_review' | 'approve_review' | 'complete'

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
