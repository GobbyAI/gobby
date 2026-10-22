import type { CanonicalTaskState, OwnerSessionRef } from "../lib/taskState";

export type { OwnerSessionRef };

export interface LifecycleTask {
  id: string;
  title: string;
  task_type: string;
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
  escalation_reason?: string | null;
  escalated_at?: string | null;
  is_escalated?: boolean;
  ref?: string;
  priority?: number | null;
  owner_session_ref?: unknown | null;
}

export interface GobbyTask extends LifecycleTask {
  id: string;
  ref: string;
  title: string;
  status: string;
  state?: CanonicalTaskState | null;
  priority: number;
  task_type: string;
  parent_task_id: string | null;
  created_at: string;
  updated_at: string;
  seq_num: number | null;
  path_cache: string | null;
  requires_user_review?: boolean;
  agent_name: string | null;
  sequence_order: number | null;
  start_date: string | null;
  due_date: string | null;
  project_id: string;
  claimed_by_session_id?: string | null;
  owner_session_ref?: OwnerSessionRef | null;
  closed_at?: string | null;
  closed_in_session_id?: string | null;
  escalated_at?: string | null;
  pre_escalation_status?: string | null;
  category?: string | null;
  additional_skills?: string[] | null;
  assigned_agent?: string | null;
}

export interface GobbyTaskDetail extends GobbyTask {
  description: string | null;
  labels: string[] | null;
  category: string | null;
  validation_status: string | null;
  validation_feedback: string | null;
  validation_criteria: string | null;
  validation_fail_count: number;
  validation_override_reason: string | null;
  closed_at: string | null;
  closed_reason: string | null;
  closed_commit_sha: string | null;
  commits: string[] | null;
  escalated_at: string | null;
  escalation_reason: string | null;
  pre_escalation_status?: string | null;
  created_in_session_id: string | null;
  closed_in_session_id: string | null;
  complexity_score: number | null;
  is_expanded: boolean;
  expansion_status: string;
  additional_skills?: string[] | null;
  assigned_agent?: string | null;
}

export interface TaskFilters {
  status: string | null;
  priority: number | null;
  taskType: string | null;
  label: string | null;
  parentTaskId: string | null;
  search: string;
  projectId?: string | null;
}

export interface TaskStats {
  [status: string]: number;
}

export interface TaskListResponse {
  tasks: GobbyTask[];
  total: number;
  stats: TaskStats;
  limit: number;
  offset: number;
}

export interface DependencyTree {
  id: string;
  ref?: string;
  title?: string;
  task_type?: string;
  blockers?: DependencyTree[];
  blocking?: DependencyTree[];
  _truncated?: boolean;
}
