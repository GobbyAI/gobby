// Shared badge components for the task system.
// Reusable across TasksPage, TaskDetail, Kanban cards, etc.

import type { TaskStateLike } from '../../lib/taskState'
import {
  getTaskDisplayState,
  getTaskStateSummary,
  getTaskStateTokens,
  TASK_STATE_COLORS,
  TASK_STATE_LABELS,
  TASK_STATE_ORDER,
  type TaskDisplayState,
} from '../../lib/taskState'
import { cn } from '../../lib/utils'

// =============================================================================
// Color maps
// =============================================================================

const STATUS_COLORS: Record<string, string> = {
  open: TASK_STATE_COLORS.ready,
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

const TASK_BADGE_CLS =
  'inline-flex items-center justify-center h-5 px-1.5 rounded-full text-[length:var(--text-2xs)] font-semibold leading-none whitespace-nowrap'
const TASK_BADGE_DOT_CLS = 'inline-block w-[7px] h-[7px] rounded-full shrink-0'
const TASK_BADGE_DOT_STANDALONE_CLS = 'inline-block w-2 h-2 rounded-full shrink-0'
const TASK_BADGE_BLOCKED_CLS =
  'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[length:var(--text-2xs)] font-medium text-[var(--color-error)] bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)]'

function chipToken(value: string | number): string {
  return String(value).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

// =============================================================================
// StatusBadge
// =============================================================================

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cn(TASK_BADGE_CLS, `chip chip--state-${chipToken(status)}`)}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

// =============================================================================
// StatusDot (minimal dot-only variant)
// =============================================================================

export function StatusDot({ status, task }: { status?: string; task?: TaskStateLike }) {
  const isKnownState = Boolean(status && TASK_STATE_ORDER.includes(status as TaskDisplayState))
  const displayState = task
    ? getTaskDisplayState(task)
    : isKnownState
      ? status as TaskDisplayState
      : getTaskDisplayState({ status })
  const label = task
    ? getTaskStateSummary(task)
    : isKnownState
      ? TASK_STATE_LABELS[status as TaskDisplayState]
      : (status ?? 'unknown').replace(/_/g, ' ')
  return (
    <span
      className={TASK_BADGE_DOT_STANDALONE_CLS}
      style={{ backgroundColor: TASK_STATE_COLORS[displayState] || "var(--text-muted)" }}
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
          className={cn(TASK_BADGE_CLS, `chip chip--state-${chipToken(token.key)}`)}
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
    <span className={cn(TASK_BADGE_CLS, `chip chip--priority-${chipToken(normalizedPriority)}`)}>
      {style.label}
    </span>
  );
}

// =============================================================================
// TypeBadge
// =============================================================================

export function TypeBadge({ type }: { type: string }) {
  return (
    <span className={cn(TASK_BADGE_CLS, `chip chip--type-${chipToken(type)}`)}>
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
      className={TASK_BADGE_BLOCKED_CLS}
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

export { STATUS_COLORS, PRIORITY_STYLES, TYPE_STYLES, TASK_BADGE_CLS, TASK_BADGE_DOT_CLS, TASK_BADGE_DOT_STANDALONE_CLS };
