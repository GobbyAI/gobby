// Shared badge components for the task system.
// Reusable across TasksPage, TaskDetail, Kanban cards, etc.

import type { TaskStateLike } from '../../lib/taskState'
import {
  getTaskBucket,
  getTaskStateSummary,
  getTaskStateTokens,
  TASK_BUCKET_BG,
  TASK_BUCKET_COLORS,
  TASK_BUCKET_LABELS,
  TASK_BUCKET_ORDER,
  type TaskBucket,
} from '../../lib/taskState'

// =============================================================================
// Color maps
// =============================================================================

const STATUS_COLORS: Record<string, string> = {
  open: TASK_BUCKET_COLORS.ready,
  in_progress: "#fb923c",
  needs_review: "#c084fc",
  review_approved: "#2dd4bf",
  closed: "#9ca3af",
  escalated: "#f87171",
};

const STATUS_BG: Record<string, string> = {
  open: TASK_BUCKET_BG.ready,
  in_progress: "rgba(251, 146, 60, 0.15)",
  needs_review: "rgba(192, 132, 252, 0.15)",
  review_approved: "rgba(45, 212, 191, 0.15)",
  closed: "rgba(156, 163, 175, 0.15)",
  escalated: "rgba(248, 113, 113, 0.15)",
};

const PRIORITY_STYLES: Record<
  number,
  { bg: string; color: string; label: string }
> = {
  0: { bg: "rgba(239, 68, 68, 0.15)", color: "#f87171", label: "Critical" },
  1: { bg: "rgba(245, 158, 11, 0.15)", color: "#fbbf24", label: "High" },
  2: { bg: "rgba(59, 130, 246, 0.12)", color: "#60a5fa", label: "Medium" },
  3: { bg: "rgba(34, 197, 94, 0.12)", color: "#4ade80", label: "Low" },
  4: { bg: "rgba(115, 115, 115, 0.15)", color: "#a3a3a3", label: "Backlog" },
};

const TYPE_STYLES: Record<string, { bg: string; color: string }> = {
  task: { bg: "rgba(59, 130, 246, 0.12)", color: "#60a5fa" },
  bug: { bg: "rgba(239, 68, 68, 0.12)", color: "#f87171" },
  feature: { bg: "rgba(34, 197, 94, 0.12)", color: "#4ade80" },
  epic: { bg: "rgba(139, 92, 246, 0.12)", color: "#a78bfa" },
  chore: { bg: "rgba(115, 115, 115, 0.15)", color: "#a3a3a3" },
};

// =============================================================================
// StatusBadge
// =============================================================================

export function StatusBadge({ status }: { status: string }) {
  const color = STATUS_COLORS[status] || "#737373";
  const bg = STATUS_BG[status] || "rgba(115, 115, 115, 0.12)";
  return (
    <span
      className="task-badge task-badge--status"
      style={{ background: bg, color }}
    >
      <span className="task-badge-dot" style={{ backgroundColor: color }} />
      {status.replace(/_/g, " ")}
    </span>
  );
}

// =============================================================================
// StatusDot (minimal dot-only variant)
// =============================================================================

export function StatusDot({ status, task }: { status?: string; task?: TaskStateLike }) {
  const isBucketStatus = Boolean(status && TASK_BUCKET_ORDER.includes(status as TaskBucket))
  const bucket = task
    ? getTaskBucket(task)
    : isBucketStatus
      ? status as TaskBucket
      : getTaskBucket({ status })
  const label = task
    ? getTaskStateSummary(task)
    : isBucketStatus
      ? TASK_BUCKET_LABELS[status as TaskBucket]
      : (status ?? 'unknown').replace(/_/g, ' ')
  return (
    <span
      className="task-badge-dot task-badge-dot--standalone"
      style={{ backgroundColor: TASK_BUCKET_COLORS[bucket] || "#737373" }}
      title={label}
      aria-label={`Status: ${label}`}
    />
  );
}

// =============================================================================
// Canonical task-state badge group
// =============================================================================

export function TaskStateBadges({ task }: { task: TaskStateLike }) {
  const tokens = getTaskStateTokens(task)

  return (
    <>
      {tokens.map(token => (
        <span
          key={token.key}
          className="task-badge task-badge--status"
          style={{ background: token.background, color: token.color }}
        >
          <span className="task-badge-dot" style={{ backgroundColor: token.color }} />
          {token.label}
        </span>
      ))}
    </>
  )
}

// =============================================================================
// PriorityBadge
// =============================================================================

export function PriorityBadge({ priority }: { priority: number }) {
  const style = PRIORITY_STYLES[priority] || PRIORITY_STYLES[2];
  return (
    <span
      className="task-badge task-badge--priority"
      style={{ background: style.bg, color: style.color }}
    >
      {style.label}
    </span>
  );
}

// =============================================================================
// TypeBadge
// =============================================================================

export function TypeBadge({ type }: { type: string }) {
  const style = TYPE_STYLES[type] || TYPE_STYLES.task;
  return (
    <span
      className="task-badge task-badge--type"
      style={{ background: style.bg, color: style.color }}
    >
      {type}
    </span>
  );
}

// =============================================================================
// BlockedIndicator
// =============================================================================

function LockIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="11" width="18" height="11" rx="2" ry="2" />
      <path d="M7 11V7a5 5 0 0 1 10 0v4" />
    </svg>
  );
}

export function BlockedIndicator({ count }: { count?: number }) {
  return (
    <span
      className="task-badge task-badge--blocked"
      title={`Blocked by ${count ?? "?"} task(s)`}
      aria-label={`Blocked by ${count ?? "unknown"} task(s)`}
    >
      <LockIcon />
      {count !== undefined && count > 0 && <span>{count}</span>}
    </span>
  );
}

// =============================================================================
// Re-export color constants for use in other components
// =============================================================================

export { STATUS_COLORS, PRIORITY_STYLES, TYPE_STYLES };
