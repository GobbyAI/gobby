import type { GobbyTaskDetail } from "../../../hooks/useTasks";
import type { StageStateView } from "../../../lib/stageActions";

function makeStage(overrides: Partial<StageStateView> = {}): StageStateView {
  return {
    name: "development",
    display_name: "Development",
    category: "implementation",
    state: "in_progress",
    review_policy: "required",
    updated_at: "2026-05-14T18:00:00Z",
    position: 10,
    ...overrides,
  };
}

export function makeTask(
  overrides: Partial<GobbyTaskDetail> = {},
): GobbyTaskDetail {
  const baseStage = makeStage();
  return {
    id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    ref: "#101",
    title: "Sample task",
    status: "in_progress",
    state: null,
    compat: null,
    priority: 2,
    task_type: "epic",
    parent_task_id: null,
    created_at: "2026-05-14T17:00:00Z",
    updated_at: "2026-05-14T17:55:00Z",
    seq_num: 101,
    path_cache: "src/secret/path/to/file.ts",
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    claimed_by_session_id: null,
    owner_session_ref: null,
    current_stage: baseStage,
    stages: [baseStage],
    description: null,
    labels: null,
    category: "code",
    validation_status: null,
    validation_feedback: null,
    validation_criteria: null,
    validation_fail_count: 0,
    validation_override_reason: null,
    closed_at: null,
    closed_reason: null,
    closed_commit_sha: null,
    commits: null,
    escalated_at: null,
    escalation_reason: null,
    pre_escalation_status: null,
    created_in_session_id: null,
    closed_in_session_id: null,
    complexity_score: null,
    is_expanded: false,
    expansion_status: "none",
    github_pr_number: null,
    github_repo: null,
    allow_automation: null,
    yolo: null,
    isolation: null,
    ...overrides,
  };
}
