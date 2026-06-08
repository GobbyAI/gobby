import { memo } from 'react'
import type { Artifact } from '../../types/artifacts'
import type { ApprovalOption } from '../../types/chat'
import { cn } from '../../lib/utils'
import { Markdown } from '../chat/Markdown'
import { PlanApprovalActions } from '../chat/PlanApprovalActions'
import {
  getPlanPendingColors,
  type PlanPendingVariant,
} from '../chat/planPendingSurface'
import { useIsMobile } from '../../hooks/useIsMobile'

interface PlanReviewCardProps {
  plan: Artifact
  planPendingApproval: boolean
  /** Authoritative approval signal from chat state (backend plan_approved). */
  planApproved?: boolean
  planApprovalOptions?: ApprovalOption[]
  onApprovePlan?: (option?: ApprovalOption) => void
  onRequestPlanChanges?: (feedback: string) => void
  onSetVersion: (id: string, index: number) => void
  planPendingVariant?: PlanPendingVariant
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
  planApproved = false,
  planApprovalOptions,
  onApprovePlan,
  onRequestPlanChanges,
  onSetVersion,
  planPendingVariant,
}: PlanReviewCardProps) {
  // Approval actions live on the agent status bar on desktop; the panel
  // carries them only on mobile, where the status bar may be off-screen.
  const isMobile = useIsMobile()

  // `planApproved` is the authoritative approval signal from chat state (set
  // only by the backend plan_approved event). Deriving it from a bare
  // pending -> not-pending edge could not tell approve from reject, so a
  // Request Changes from the desktop status bar misrendered as "Plan
  // approved" (#15681 — the symmetric half of #15663).
  const status: ReviewStatus = planPendingApproval
    ? 'pending'
    : planApproved
      ? 'approved'
      : 'idle'

  const version = plan.versions.length > 0
    ? plan.versions[plan.currentVersionIndex] ?? plan.versions[plan.versions.length - 1]
    : undefined
  const content = version?.content ?? ''
  const planPendingColors = getPlanPendingColors(planPendingVariant)

  const handleRequestChanges = (feedback: string) => {
    onRequestPlanChanges?.(feedback)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {status === 'pending' && (
        <div
          data-testid="plan-review-status"
          data-status="pending"
          className={cn(
            'flex flex-col gap-3 border-b px-4 py-3',
            planPendingColors.surfaceBg,
            planPendingColors.borderColor,
          )}
        >
          <div className="flex items-center gap-2">
            <ClockIcon className={cn('shrink-0', planPendingColors.accentText)} />
            <span className={cn('text-sm font-semibold', planPendingColors.accentText)}>
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
              options={planApprovalOptions}
              layout="stacked"
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
