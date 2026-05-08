export const CURRENT_DB_SESSION_ID = "web-main-current";
export const CURRENT_REF = "#202";
export const TASK_LINKED_DB_SESSION_ID = "web-task-linked";
export const OTHER_DB_SESSION_ID = "web-other";
export const STALE_TERMINAL_SESSION_ID = "terminal-stale";
export const CLAIMED_TASK_ID = "task-14425";
export const CLAIMED_TASK_REF = 14425;

export const inProgressStage = {
  name: "development",
  display_name: "Development",
  category: "development",
  state: "in_progress",
  review_policy: "none",
  updated_at: "2026-04-19T18:30:00Z",
  position: 1,
};

const CLAIMED_TASK_BASE = {
  id: CLAIMED_TASK_ID,
  ref: `#${CLAIMED_TASK_REF}`,
  title: "Verify web chat task state survives refresh",
  status: "in_progress",
  priority: 2,
  task_type: "bug",
  parent_task_id: null,
  created_at: "2026-04-19T18:30:00Z",
  updated_at: "2026-04-19T18:35:00Z",
  seq_num: CLAIMED_TASK_REF,
  path_cache: String(CLAIMED_TASK_REF),
  assignee: null,
  agent_name: null,
  sequence_order: null,
  start_date: null,
  due_date: null,
  project_id: "proj-1",
  claimed_by_session_id: CURRENT_DB_SESSION_ID,
  category: "test",
  labels: ["web-chat", "tasks", "persistence"],
  description: "Refresh should rehydrate task ownership and task-linked session state.",
  validation_status: "pending",
  validation_feedback: null,
  validation_criteria: "Refresh keeps claimed task state visible.",
  validation_fail_count: 0,
  validation_override_reason: null,
  closed_at: null,
  closed_reason: null,
  closed_commit_sha: null,
  commits: [],
  closed_in_session_id: null,
  escalated_at: null,
  escalation_reason: null,
  pre_escalation_status: null,
  created_in_session_id: CURRENT_DB_SESSION_ID,
  complexity_score: null,
  is_expanded: false,
  expansion_status: "not_expanded",
  github_pr_number: null,
  github_repo: null,
  allow_automation: false,
  yolo: false,
  isolation: "worktree",
  dispatch_failure_count: 0,
  additional_skills: [],
  assigned_agent: null,
  current_stage: inProgressStage,
  stages: [
    {
      name: "planning",
      display_name: "Planning",
      category: "development",
      state: "done",
      review_policy: "none",
      updated_at: "2026-04-19T18:29:00Z",
      position: 0,
    },
    inProgressStage,
  ],
  state: {
    owner_session_id: CURRENT_DB_SESSION_ID,
    current_stage: inProgressStage,
    is_claimed: true,
    is_closed: false,
    is_escalated: false,
    is_blocked: false,
    is_merge_ready: false,
    closed_at: null,
    closed_reason: null,
    closed_in_session_id: null,
    closed_commit_sha: null,
    escalated_at: null,
    escalation_reason: null,
  },
};

export type ClaimedTaskFixture = typeof CLAIMED_TASK_BASE;

export function createClaimedTask(
  overrides: Partial<ClaimedTaskFixture> = {},
): ClaimedTaskFixture {
  return { ...CLAIMED_TASK_BASE, ...overrides };
}
