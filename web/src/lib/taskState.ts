import {
  ActivityGlyph,
  CheckGlyph,
  CircleGlyph,
  DashGlyph,
  ErrorGlyph,
  EyeGlyph,
  LockGlyph,
  type StatusGlyph,
  type StatusKind,
} from "../components/activity/ActivityRowStatusDot";

export type TaskDisplayState =
  | "open"
  | "in_progress"
  | "blocked"
  | "needs_human"
  | "escalated"
  | "deferred"
  | "closed";

/**
 * Friendly, human-readable owner-session identity resolved by the backend
 * tasks serializer. The web UI renders `ref` (`#<seq_num>` or a short hash)
 * and `source` — never the raw 36-char `session_id` UUID.
 */
export interface OwnerSessionRef {
  session_id: string;
  ref: string;
  source: string | null;
}

export interface CanonicalTaskState {
  name: TaskDisplayState;
  hold_state: "needs_human" | "deferred" | null;
  follow_up_at: string | null;
  owner_session_id: string | null;
  owner_session_ref: OwnerSessionRef | null;

  is_claimed: boolean;
  is_closed: boolean;
  is_escalated: boolean;
  is_blocked: boolean;

  closed_at: string | null;
  closed_reason: string | null;
  closed_in_session_id: string | null;
  closed_commit_sha: string | null;
  escalated_at: string | null;
  escalation_reason: string | null;
}

export interface TaskStateLike {
  claimed_by_session_id?: string | null;
  owner_session_ref?: OwnerSessionRef | null;
  closed_at?: string | null;
  closed_reason?: string | null;
  closed_in_session_id?: string | null;
  closed_commit_sha?: string | null;
  escalated_at?: string | null;
  escalation_reason?: string | null;
  is_blocked?: boolean | null;

  state?: Partial<CanonicalTaskState> | null;
}

export interface TaskStateBadgeToken {
  key: string;
  label: string;
  color: string;
  background: string;
}

export const TASK_STATE_ORDER: TaskDisplayState[] = [
  "open",
  "in_progress",
  "blocked",
  "needs_human",
  "escalated",
  "deferred",
  "closed",
];

export const TASK_STATE_LABELS: Record<TaskDisplayState, string> =
  {
    open: "Open",
    in_progress: "In Progress",
    blocked: "Blocked",
    needs_human: "Needs Human",
    escalated: "Escalated",
    deferred: "Deferred",
    closed: "Closed",
  };

export const TASK_STATE_COLORS: Record<TaskDisplayState, string> = {
  open: "var(--color-info)",
  in_progress: "var(--color-warning-foreground)",

  blocked: "var(--color-error)",
  needs_human: "var(--color-warning-foreground)",
  escalated: "var(--color-error)",
  deferred: "var(--text-muted)",
  closed: "var(--text-muted)",
};

export const TASK_STATE_KIND: Record<TaskDisplayState, StatusKind> = {
  open: "info",
  in_progress: "warning",

  blocked: "error",
  needs_human: "warning",
  escalated: "error",
  deferred: "disabled",
  closed: "disabled",
};

// Shapes distinguish task states without relying on color.
export const TASK_STATE_GLYPH: Record<TaskDisplayState, StatusGlyph> = {
  open: CircleGlyph,
  in_progress: ActivityGlyph,

  blocked: LockGlyph,
  needs_human: EyeGlyph,
  escalated: ErrorGlyph,
  deferred: DashGlyph,
  closed: CheckGlyph,
};

export const TASK_STATE_BG: Record<TaskDisplayState, string> = {
  open: "var(--color-info-soft)",
  in_progress: "var(--color-warning-soft)",

  blocked: "var(--color-error-soft)",
  needs_human: "var(--color-warning-soft)",
  escalated: "var(--color-error-soft)",
  deferred: "color-mix(in srgb, var(--text-muted) 15%, transparent)",
  closed: "color-mix(in srgb, var(--text-muted) 15%, transparent)",
};

export function getCanonicalTaskState(task: TaskStateLike): CanonicalTaskState {
  const ownerSessionId =
    task.state?.owner_session_id ?? task.claimed_by_session_id ?? null;
  const ownerSessionRef =
    task.state?.owner_session_ref ?? task.owner_session_ref ?? null;
  const isClaimed = task.state?.is_claimed ?? Boolean(ownerSessionId);
  const isClosed = task.state?.is_closed ?? Boolean(task.closed_at);
  const isEscalated =
    !isClosed && (task.state?.is_escalated ?? Boolean(task.escalated_at));
  const isBlocked =
    !isClosed && (task.state?.is_blocked ?? task.is_blocked ?? false);
  const holdState = task.state?.hold_state ?? null;
  const name = task.state?.name ?? (
    isClosed ? "closed" :
    isEscalated ? "escalated" :
    holdState ?? (isBlocked ? "blocked" : isClaimed ? "in_progress" : "open")
  );

  return {
    name,
    hold_state: holdState,
    follow_up_at: task.state?.follow_up_at ?? null,
    owner_session_id: ownerSessionId,
    owner_session_ref: ownerSessionRef,

    is_claimed: isClaimed,
    is_closed: name === "closed",
    is_escalated: name === "escalated",
    is_blocked: name === "blocked",

    closed_at: task.state?.closed_at ?? task.closed_at ?? null,
    closed_reason: task.state?.closed_reason ?? task.closed_reason ?? null,
    closed_in_session_id:
      task.state?.closed_in_session_id ?? task.closed_in_session_id ?? null,
    closed_commit_sha:
      task.state?.closed_commit_sha ?? task.closed_commit_sha ?? null,
    escalated_at: task.state?.escalated_at ?? task.escalated_at ?? null,
    escalation_reason:
      task.state?.escalation_reason ?? task.escalation_reason ?? null,
  };
}

export function getTaskDisplayState(task: TaskStateLike): TaskDisplayState {
  return getCanonicalTaskState(task).name;
}

export function isTaskClosed(task: TaskStateLike): boolean {
  return getTaskDisplayState(task) === "closed";
}

export function getTaskStateLabel(task: TaskStateLike): string {
  return TASK_STATE_LABELS[getTaskDisplayState(task)];
}

export function getTaskStateSummary(task: TaskStateLike): string {
  const state = getCanonicalTaskState(task);
  const parts = [getTaskStateLabel(task)];

  if (!state.is_closed && state.is_escalated) {
    parts.push("Escalated");
  }
  if (!state.is_closed && state.is_claimed) {
    parts.push("Claimed");
  }

  return [...new Set(parts)].join(" · ");
}

export function getTaskStateTokens(task: TaskStateLike): TaskStateBadgeToken[] {
  const displayState = getTaskDisplayState(task);
  const state = getCanonicalTaskState(task);
  const tokens: TaskStateBadgeToken[] = [
    {
      key: displayState,
      label: getTaskStateLabel(task),
      color: TASK_STATE_COLORS[displayState],
      background: TASK_STATE_BG[displayState],
    },
  ];

  if (!state.is_closed && state.is_claimed) {
    tokens.push({
      key: "claimed",
      label: "Claimed",
      color: "var(--text-secondary)",
      background: "color-mix(in srgb, var(--text-secondary) 14%, transparent)",
    });
  }

  return tokens;
}

export function countTasksByState(
  tasks: TaskStateLike[],
): Record<TaskDisplayState, number> {
  const counts: Record<TaskDisplayState, number> = {
    open: 0,
    in_progress: 0,
    blocked: 0,
    needs_human: 0,
    escalated: 0,
    deferred: 0,
    closed: 0,
  };

  for (const task of tasks) {
    counts[getTaskDisplayState(task)] += 1;
  }

  return counts;
}

export function matchesTaskStateFilter(
  task: TaskStateLike,
  filter: string | null,
): boolean {
  if (!filter) return true;

  const displayState = getTaskDisplayState(task);
  switch (filter) {
    case "recently_done":
    case "closed":
      return displayState === "closed";
    default:
      return displayState === filter;
  }
}
