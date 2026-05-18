// Shared badge components for the task system.
// Reusable across the task tree, board cards, and the detail panel.

import './task-execution.css'

import type { TaskStateLike } from '../../lib/taskState'
import {
  getTaskDisplayState,
  getTaskStateSummary,
  getTaskStateTokens,
  TASK_STATE_COLORS,
  TASK_STATE_GLYPH,
  TASK_STATE_KIND,
  TASK_STATE_LABELS,
  TASK_STATE_ORDER,
  type TaskDisplayState,
} from '../../lib/taskState'
import { ActivityRowStatusDot } from '../activity/ActivityRowStatusDot'
import { taskPriorityLabel } from '../../lib/taskOptions'
import { cn } from '../../lib/utils'
import {
  PRIORITY_GLYPH_PATHS,
  normalizePriorityGlyphLevel,
  type PriorityGlyphLevel,
} from './priorityGlyphPaths'

// =============================================================================
// Color maps
// =============================================================================

const STATUS_COLORS: Record<string, string> = {
  open: TASK_STATE_COLORS.ready,
  in_progress: "var(--color-warning-foreground)",
  needs_review: "var(--color-info)",
  review_approved: "var(--color-review)",
  closed: "var(--text-muted)",
  escalated: "var(--color-error)",
};

const PRIORITY_STYLES: Record<
  PriorityGlyphLevel,
  { bg: string; color: string }
> = {
  0: { bg: "var(--color-error-soft)", color: "var(--color-error)" },
  1: { bg: "var(--color-warning-soft)", color: "var(--color-warning-foreground)" },
  2: { bg: "var(--color-info-soft)", color: "var(--color-info)" },
  3: { bg: "var(--color-success-soft)", color: "var(--color-success-foreground)" },
  4: { bg: "color-mix(in srgb, var(--text-muted) 15%, transparent)", color: "var(--text-muted)" },
};

const TASK_BADGE_CLS =
  'inline-flex items-center justify-center h-5 px-1.5 rounded-full text-[length:var(--text-2xs)] font-semibold leading-none whitespace-nowrap'
const TASK_BADGE_DOT_CLS = 'inline-block w-[7px] h-[7px] rounded-full shrink-0'
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

function displayStateFromStatus(status: string | undefined): TaskDisplayState {
  if (status === 'open') return 'ready'
  if (status && TASK_STATE_ORDER.includes(status as TaskDisplayState)) {
    return status as TaskDisplayState
  }
  return 'ready'
}

export function StatusDot({ status, task }: { status?: string; task?: TaskStateLike }) {
  const displayState = task ? getTaskDisplayState(task) : displayStateFromStatus(status)
  const label = task
    ? getTaskStateSummary(task)
    : TASK_STATE_LABELS[displayState]
  return (
    <ActivityRowStatusDot
      kind={TASK_STATE_KIND[displayState]}
      glyph={TASK_STATE_GLYPH[displayState]}
      label={`Status: ${label}`}
      title={label}
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
  const normalizedPriority = normalizePriorityGlyphLevel(priority);
  const label = taskPriorityLabel(normalizedPriority) ?? "Medium";
  return (
    <span className={cn(TASK_BADGE_CLS, `chip chip--priority-${chipToken(normalizedPriority)}`)}>
      {label}
    </span>
  );
}

// =============================================================================
// PriorityGlyph — compact, deutan-safe priority symbol for dense rows
// =============================================================================

export function PriorityGlyph({ priority }: { priority: number }) {
  const p = normalizePriorityGlyphLevel(priority);
  const style = PRIORITY_STYLES[p];
  const label = taskPriorityLabel(p) ?? "Medium";
  const filled = p <= 1;
  return (
    <span
      className="priority-glyph"
      style={{ color: style.color }}
      title={`${label} priority`}
      aria-label={`${label} priority`}
      role="img"
    >
      <svg width="10" height="11" viewBox="0 0 10 11" aria-hidden="true">
        <path
          d={PRIORITY_GLYPH_PATHS[p]}
          fill={filled ? "currentColor" : "none"}
          stroke="currentColor"
          strokeWidth={filled ? 0 : 1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
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

export {
  STATUS_COLORS,
  PRIORITY_STYLES,
  TASK_BADGE_BLOCKED_CLS,
  TASK_BADGE_CLS,
  TASK_BADGE_DOT_CLS,
};
