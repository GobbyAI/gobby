export type StageState5 =
  "ready" | "in_progress" | "needs_review" | "review_approved" | "done";

export type StageRowState = StageState5;

export type ReviewPolicy = "none" | "required" | "optional";

export interface StageStateView {
  name: string;
  display_name: string;
  category: string;
  state: StageState5;
  review_policy: ReviewPolicy;
  updated_at: string | null;
  position?: number | null;
  reviewer_agent?: string | null;
  work_attempt_count?: number | null;
  review_round_count?: number | null;
  max_work_attempts?: number | null;
  max_review_rounds?: number | null;
  artifact_refs?: Record<string, string> | null;
  entered_at?: string | null;
  entered_by_session_id?: string | null;
  completed_at?: string | null;
  completed_by_session_id?: string | null;
  completed_commit_sha?: string | null;
  notes?: string | null;
}

export interface LifecycleTask {
  id: string;
  title: string;
  task_type: string;
  stages: StageStateView[];
  state?: {
    owner_session_id?: string | null;
    owner_session_ref?: unknown | null;
    is_claimed?: boolean;
    is_closed?: boolean;
    is_escalated?: boolean;
    is_blocked?: boolean;
    blocked_reason?: string | null;
    closed_at?: string | null;
    closed_reason?: string | null;
    closed_in_session_id?: string | null;
    closed_commit_sha?: string | null;
    escalated_at?: string | null;
    escalation_reason?: string | null;
  } | null;
  is_blocked?: boolean;
  blocked_reason?: string | null;
  claimed_by_session_id?: string | null;
  closed_at?: string | null;
  closed_reason?: string | null;
  closed_in_session_id?: string | null;
  closed_commit_sha?: string | null;
  validation_fail_count?: number | null;
  dispatch_failure_count?: number | null;
  escalation_reason?: string | null;
  escalated_at?: string | null;
  is_escalated?: boolean;
  ref?: string;
  priority?: number | null;
  owner_session_ref?: unknown | null;
}

interface CurrentStageRow {
  name?: string | null;
  stage_name?: string | null;
  position?: number | null;
  state?: string | null;
}

interface CurrentStageTask<TStage extends CurrentStageRow> {
  stages?: readonly TStage[] | null;
  state?: object | null;
  current_stage?: TStage | null;
}

export function currentStage<TStage extends CurrentStageRow>(
  task: CurrentStageTask<TStage> | null | undefined,
): TStage | null {
  if (!task) return null;
  if (Array.isArray(task.stages)) {
    if (task.stages.length > 0) {
      return (
        [...task.stages]
          .sort((a, b) => {
            const positionDifference = (a.position ?? 0) - (b.position ?? 0);
            if (positionDifference !== 0) return positionDifference;
            const aName = a.name ?? a.stage_name ?? "";
            const bName = b.name ?? b.stage_name ?? "";
            return aName < bName ? -1 : aName > bName ? 1 : 0;
          })
          .find((row) => row.state !== "done") ?? null
      );
    }
  }
  const state = task.state as
    { current_stage?: TStage | null } | null | undefined;
  return state?.current_stage ?? task.current_stage ?? null;
}
