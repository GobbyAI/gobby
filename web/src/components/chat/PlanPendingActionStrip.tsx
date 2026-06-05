import type { ApprovalOption } from '../../types/chat'
import { cn } from '../../lib/utils'
import { Button } from '../shared/Button'
import { PlanApprovalActions } from './PlanApprovalActions'

interface PlanPendingActionStripProps {
  onApprove: (option?: ApprovalOption) => void
  onRequestChanges: (feedback: string) => void
  /** Per-CLI plan-accept options; empty/absent falls back to a single Approve. */
  options?: ApprovalOption[]
  /** Focus the Plans panel (altitude-3 canonical view). */
  onView?: () => void
  className?: string
}

/**
 * Altitude-2 glanceable-action surface: a compact warning-state strip with
 * Approve / Request-changes / View, shown as the pending state of the agent
 * status bar (and reused in the mobile chat view). Warning token + clock icon,
 * no side-stripe accent per .impeccable.md BAN 1; reuses the shared actions.
 */
export function PlanPendingActionStrip({
  onApprove,
  onRequestChanges,
  options,
  onView,
  className,
}: PlanPendingActionStripProps) {
  return (
    <div
      data-testid="plan-pending-strip"
      className={cn(
        'flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-[color-mix(in_srgb,var(--color-warning-foreground)_24%,transparent)] bg-[var(--color-warning-soft)] px-2.5 py-1.5',
        className,
      )}
    >
      <span className="flex items-center gap-1.5 text-xs font-semibold text-[var(--color-warning-foreground)]">
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
