// Shared badge components for the task system.
// Reusable across TasksPage, TaskDetail, Kanban cards, etc.

import type { TaskStateLike } from '../../lib/taskState'
import {
  getTaskBucket,
  getTaskStateSummary,
  getTaskStateTokens,
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

function chipToken(value: string | number): string {
  return String(value).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

// =============================================================================
// StatusBadge
// =============================================================================

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`task-badge chip chip--state-${chipToken(status)}`}>
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
          className={`task-badge chip chip--state-${chipToken(token.key)}`}
        >
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
  const normalizedPriority = priority in PRIORITY_STYLES ? priority : 2;
  const style = PRIORITY_STYLES[normalizedPriority];
  return (
    <span className={`task-badge chip chip--priority-${chipToken(normalizedPriority)}`}>
      {style.label}
    </span>
  );
}

// =============================================================================
// TypeBadge
// =============================================================================

export function TypeBadge({ type }: { type: string }) {
  return (
    <span className={`task-badge chip chip--type-${chipToken(type)}`}>
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
