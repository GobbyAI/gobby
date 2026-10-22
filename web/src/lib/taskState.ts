import {
  ActivityGlyph,
  CircleGlyph,
  DashGlyph,
  LockGlyph,
  type StatusGlyph,
  type StatusKind,
} from "../components/activity/ActivityRowStatusDot";

export type TaskDisplayState =
  | "ready"
  | "in_progress"
  | "blocked"
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
  "ready",
  "in_progress",

  "blocked",

  "closed",
];

export const TASK_STATE_LABELS: Record<TaskDisplayState, string> =
  {
    ready: "Ready",
    in_progress: "In Progress",

    blocked: "Blocked",

    closed: "Closed",
  };

export const TASK_STATE_COLORS: Record<TaskDisplayState, string> = {
  ready: "var(--color-info)",
  in_progress: "var(--color-warning-foreground)",

  blocked: "var(--color-error)",

  closed: "var(--text-muted)",
};

export const TASK_STATE_KIND: Record<TaskDisplayState, StatusKind> = {
  ready: "info",
  in_progress: "warning",

  blocked: "error",

  closed: "disabled",
};

// Shapes distinguish task states without relying on color.
export const TASK_STATE_GLYPH: Record<TaskDisplayState, StatusGlyph> = {
  ready: CircleGlyph,
  in_progress: ActivityGlyph,

  blocked: LockGlyph,

  closed: DashGlyph,
};

export const TASK_STATE_BG: Record<TaskDisplayState, string> = {
  ready: "var(--color-info-soft)",
  in_progress: "var(--color-warning-soft)",

  blocked: "var(--color-error-soft)",

  closed: "color-mix(in srgb, var(--text-muted) 15%, transparent)",
};

export function getCanonicalTaskState(task: TaskStateLike): CanonicalTaskState {
  const ownerSessionId =
    task.state?.owner_session_id ?? task.claimed_by_session_id ?? null;
  const ownerSessionRef =
    task.state?.owner_session_ref ?? task.owner_session_ref ?? null;
  const isClosed = task.state?.is_closed ?? Boolean(task.closed_at);
  const isEscalated =
    !isClosed && (task.state?.is_escalated ?? Boolean(task.escalated_at));
  const isBlocked =
    !isClosed && (task.state?.is_blocked ?? task.is_blocked ?? isEscalated);

  return {
    owner_session_id: ownerSessionId,
    owner_session_ref: ownerSessionRef,

    is_claimed: task.state?.is_claimed ?? Boolean(ownerSessionId),
    is_closed: Boolean(isClosed),
    is_escalated: Boolean(isEscalated),
    is_blocked: Boolean(isBlocked),

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
  const state = getCanonicalTaskState(task);
  if (state.is_closed) return "closed";
  if (state.is_blocked || state.is_escalated) return "blocked";
  if (state.is_claimed) return "in_progress";

  return "ready";
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

  if (!state.is_closed && state.is_escalated) {
    tokens.push({
      key: "escalated",
      label: "Escalated",
      color: "var(--color-error)",
      background: "var(--color-error-soft)",
    });
  }

  return tokens;
}

export function countTasksByState(
  tasks: TaskStateLike[],
): Record<TaskDisplayState, number> {
  const counts: Record<TaskDisplayState, number> = {
    ready: 0,
    in_progress: 0,

    blocked: 0,

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
    case "open":
    case "ready":
      return displayState === "ready";
    case "escalated":
    case "blocked":
      return displayState === "blocked";
    case "in_progress":
      return displayState === "in_progress";
    default:
      return displayState === filter;
  }
}
