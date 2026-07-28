import type { ApprovalOption } from '../../types/chat'
import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { PlanApprovalActions } from './PlanApprovalActions'
import {
  getPlanPendingColors,
  type PlanPendingVariant,
} from './planPendingSurface'

interface PlanPendingActionStripProps {
  onApprove: (option?: ApprovalOption) => void
  onRequestChanges: (feedback: string) => void
  /** Per-CLI plan-accept options; empty/absent falls back to a single Approve. */
  options?: ApprovalOption[]
  /** Focus the Plans panel (altitude-3 canonical view). */
  onView?: () => void
  variant?: PlanPendingVariant
  className?: string
}

/**
 * Altitude-2 glanceable-action surface: a compact pending-state strip with
 * Approve / Request-changes / View, shown as the pending state of the agent
 * status bar (and reused in the mobile chat view). Shares the approval-surface
 * color treatment (`planPendingColors`) with the Plans panel header; clock icon
 * carries the state, no side-stripe accent per .impeccable.md BAN 1; reuses the
 * shared actions.
 */
export function PlanPendingActionStrip({
  onApprove,
  onRequestChanges,
  options,
  onView,
  variant,
  className,
}: PlanPendingActionStripProps) {
  const planPendingColors = getPlanPendingColors(variant)

  return (
    <div
      data-testid="plan-pending-strip"
      role="status"
      aria-live="polite"
      aria-label="Plan awaiting approval"
      className={cn(
        'flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border px-2.5 py-1.5',
        planPendingColors.surfaceBg,
        planPendingColors.borderColor,
        className,
      )}
    >
      <span
        className={cn(
          'flex items-center gap-1.5 text-xs font-semibold',
          planPendingColors.accentText,
        )}
      >
        <ClockIcon className="shrink-0" />
        Plan awaiting approval
      </span>
      <div className="ml-auto flex flex-wrap items-center gap-2">
        <PlanApprovalActions
          onApprove={onApprove}
          onRequestChanges={onRequestChanges}
          options={options}
          testIdPrefix="plan-strip"
        />
        {onView && (
          <Button size="sm" variant="ghost" onClick={onView} data-testid="plan-strip-view">
            View
          </Button>
        )}
      </div>
    </div>
  )
}

function ClockIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <polyline points="12 7 12 12 15 14" />
    </svg>
  )
}
