import { useState } from 'react'
import { cn } from '../../lib/utils'
import { Button } from '../shared/Button'
import { PlanApprovalActions } from './PlanApprovalActions'

interface PlanPendingApprovalBlockProps {
  onApprove: () => void
  onRequestChanges: (feedback: string) => void
  /** Focus the Plans panel (altitude-3 canonical view). */
  onView?: () => void
  defaultExpanded?: boolean
}

/**
 * Altitude-1 in-flow surface: a collapsible "awaiting approval" block rendered
 * inline at the end of the transcript, right after the plan turn, while a plan
 * is pending. Uses the warning state (amber token + clock icon, no side-stripe
 * accent per .impeccable.md BAN 1) and reuses the shared plan actions.
 */
export function PlanPendingApprovalBlock({
  onApprove,
  onRequestChanges,
  onView,
  defaultExpanded = true,
}: PlanPendingApprovalBlockProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)

  return (
    <div className="px-4 py-3">
      <div className="mx-auto max-w-3xl">
        <div
          data-testid="plan-pending-block"
          className="overflow-hidden rounded-lg border border-[color-mix(in_srgb,var(--color-warning-foreground)_24%,var(--border))] bg-[color-mix(in_srgb,var(--color-warning-foreground)_6%,transparent)]"
        >
          <div className="flex items-center gap-2 px-3 py-2">
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              aria-expanded={expanded}
              aria-controls="plan-pending-block-body"
              data-testid="plan-pending-toggle"
              className="flex flex-1 items-center gap-2 text-left pointer-coarse:min-h-11"
            >
              <ClockIcon className="shrink-0 text-[var(--color-warning-foreground)]" />
              <span className="text-sm font-semibold text-[var(--color-warning-foreground)]">
                Awaiting your approval
              </span>
              <ChevronDownIcon
                className={cn(
                  'ml-1 shrink-0 text-[var(--color-warning-foreground)] transition-transform',
                  expanded ? 'rotate-180' : '',
                )}
              />
            </button>
            {onView && (
              <Button
                size="sm"
                variant="ghost"
                onClick={onView}
                data-testid="plan-pending-view"
              >
                View plan
              </Button>
            )}
          </div>
          {expanded && (
            <div id="plan-pending-block-body" className="flex flex-col gap-3 px-3 pb-3">
              <p className="max-w-[70ch] text-sm text-muted-foreground">
                The agent proposed a plan. Approve it to execute, or request changes.
              </p>
              <PlanApprovalActions
                onApprove={onApprove}
                onRequestChanges={onRequestChanges}
                testIdPrefix="plan-pending"
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function ClockIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
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

function ChevronDownIcon({ className }: { className?: string }) {
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
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}
