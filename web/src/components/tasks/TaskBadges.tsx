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
  in_progress: "var(--color-warning-foreground)",
  needs_review: "var(--color-agent)",
  review_approved: "var(--color-review)",
  closed: "var(--text-muted)",
  escalated: "var(--color-error)",
};

const PRIORITY_STYLES: Record<
  number,
  { bg: string; color: string; label: string }
> = {
  0: { bg: "var(--color-error-soft)", color: "var(--color-error)", label: "Critical" },
  1: { bg: "var(--color-warning-soft)", color: "var(--color-warning-foreground)", label: "High" },
  2: { bg: "var(--color-info-soft)", color: "var(--color-info)", label: "Medium" },
  3: { bg: "var(--color-success-soft)", color: "var(--color-success-foreground)", label: "Low" },
  4: { bg: "color-mix(in srgb, var(--text-muted) 15%, transparent)", color: "var(--text-muted)", label: "Backlog" },
};

const TYPE_STYLES: Record<string, { bg: string; color: string }> = {
  task: { bg: "var(--color-info-soft)", color: "var(--color-info)" },
  bug: { bg: "var(--color-error-soft)", color: "var(--color-error)" },
  feature: { bg: "var(--color-success-soft)", color: "var(--color-success-foreground)" },
  epic: { bg: "var(--color-agent-soft)", color: "var(--color-agent)" },
  chore: { bg: "color-mix(in srgb, var(--text-muted) 15%, transparent)", color: "var(--text-muted)" },
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
      style={{ backgroundColor: TASK_BUCKET_COLORS[bucket] || "var(--text-muted)" }}
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
