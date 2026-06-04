import { memo, useState } from 'react'
import type { Artifact } from '../../types/artifacts'
import { cn } from '../../lib/utils'
import { Markdown } from '../chat/Markdown'
import { PlanApprovalActions } from '../chat/PlanApprovalActions'
import { useIsMobile } from '../../hooks/useIsMobile'

interface PlanReviewCardProps {
  plan: Artifact
  planPendingApproval: boolean
  onApprovePlan?: () => void
  onRequestPlanChanges?: (feedback: string) => void
  onSetVersion: (id: string, index: number) => void
}

type ReviewStatus = 'pending' | 'approved' | 'idle'

/**
 * Altitude-3 plan surface: the canonical Plans view.
 *
 * Renders the full untruncated plan, a warning-state "awaiting approval"
 * header with approve / request-changes actions, the reject -> revise
 * revision history, and a success-state header once the plan is approved.
 *
 * State communication follows the design contract: pending uses the amber
 * warning token plus an icon (never hue alone), approved uses the success
 * token plus a check icon, and neither relies on a side-stripe accent.
 */
export const PlanReviewCard = memo(function PlanReviewCard({
  plan,
  planPendingApproval,
  onApprovePlan,
  onRequestPlanChanges,
  onSetVersion,
}: PlanReviewCardProps) {
  // Approval actions live on the agent status bar on desktop; the panel
  // carries them only on mobile, where the status bar may be off-screen.
  const isMobile = useIsMobile()
  const [approved, setApproved] = useState(false)
  const [didRequestChanges, setDidRequestChanges] = useState(false)
  // Track previous props so derived state can be adjusted during render (the
  // React "store information from previous renders" pattern) rather than in an
  // effect, which would cascade renders.
  const [prevPlanId, setPrevPlanId] = useState(plan.id)
  const [prevPending, setPrevPending] = useState(planPendingApproval)

  // Derive the approved state from the pending -> not-pending transition.
  // A request-changes also clears pending transiently (the action eagerly
  // hides the approval UI), so suppress "approved" when the user explicitly
  // asked for changes; a fresh plan_pending_approval re-arms the cycle.
  if (plan.id !== prevPlanId) {
    // A different plan became active — start its review fresh.
    setPrevPlanId(plan.id)
    setPrevPending(planPendingApproval)
    setApproved(false)
    setDidRequestChanges(false)
  } else if (planPendingApproval !== prevPending) {
    setPrevPending(planPendingApproval)
    if (planPendingApproval) {
      setApproved(false)
      setDidRequestChanges(false)
    } else if (prevPending && !didRequestChanges) {
      setApproved(true)
    }
  }

  const status: ReviewStatus = planPendingApproval ? 'pending' : approved ? 'approved' : 'idle'

  const version = plan.versions[plan.currentVersionIndex] ?? plan.versions[plan.versions.length - 1]
  const content = version?.content ?? ''

  const handleRequestChanges = (feedback: string) => {
    setDidRequestChanges(true)
    onRequestPlanChanges?.(feedback)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {status === 'pending' && (
        <div
          data-testid="plan-review-status"
          data-status="pending"
          className="flex flex-col gap-3 border-b border-[color-mix(in_srgb,var(--color-warning-foreground)_22%,var(--border))] bg-[color-mix(in_srgb,var(--color-warning-foreground)_7%,transparent)] px-4 py-3"
        >
          <div className="flex items-center gap-2">
            <ClockIcon className="shrink-0 text-[var(--color-warning-foreground)]" />
            <span className="text-sm font-semibold text-[var(--color-warning-foreground)]">
              Awaiting your approval
            </span>
          </div>
          <p className="max-w-[70ch] text-sm text-muted-foreground">
            {isMobile
              ? 'Review the plan below, then approve it or request changes.'
              : 'Review the plan below, then approve or request changes from the status bar.'}
          </p>
          {isMobile && onApprovePlan && onRequestPlanChanges && (
            <PlanApprovalActions
              onApprove={onApprovePlan}
              onRequestChanges={handleRequestChanges}
              testIdPrefix="plan-review"
            />
          )}
        </div>
      )}

      {status === 'approved' && (
        <div
          data-testid="plan-review-status"
          data-status="approved"
          className="flex items-center gap-2 border-b border-[color-mix(in_srgb,var(--color-success-foreground)_22%,var(--border))] bg-[color-mix(in_srgb,var(--color-success-foreground)_7%,transparent)] px-4 py-3"
        >
          <CheckIcon className="shrink-0 text-[var(--color-success-foreground)]" />
          <span className="text-sm font-semibold text-[var(--color-success-foreground)]">
            Plan approved
          </span>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-auto">
        <div className="message-content prose dark:prose-invert max-w-none p-4 leading-relaxed prose-p:leading-relaxed">
          <Markdown content={content} id={`plan-review-${plan.id}`} />
        </div>

        {plan.versions.length > 1 && <RevisionHistory plan={plan} onSetVersion={onSetVersion} />}
      </div>
    </div>
  )
})

function RevisionHistory({
  plan,
  onSetVersion,
}: {
  plan: Artifact
  onSetVersion: (id: string, index: number) => void
}) {
  return (
    <section aria-label="Plan revision history" className="border-t border-border px-4 py-3">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Revision history
      </h3>
      <ol className="flex flex-col gap-1">
        {plan.versions.map((v, index) => {
          const isCurrent = index === plan.currentVersionIndex
          return (
            <li key={index}>
              <button
                type="button"
                onClick={() => onSetVersion(plan.id, index)}
                aria-current={isCurrent ? 'true' : undefined}
                className={cn(
                  'flex w-full items-baseline justify-between gap-3 rounded-md px-2 py-1.5 text-left text-sm transition-colors pointer-coarse:min-h-11',
                  isCurrent
                    ? 'bg-accent/10 font-medium text-foreground'
                    : 'text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                <span>Revision {index + 1}</span>
                <span className="font-mono text-xs text-muted-foreground">{formatTime(v.timestamp)}</span>
              </button>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function formatTime(d: Date): string {
  try {
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return ''
  }
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

function CheckIcon({ className }: { className?: string }) {
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
      <path d="M20 6 9 17l-5-5" />
    </svg>
  )
}
