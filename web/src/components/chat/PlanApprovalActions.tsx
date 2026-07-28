import { useState } from 'react'
import type { ApprovalOption } from '../../types/chat'
import { Button } from '../ui/Button'
import { cn } from '../../lib/utils'

interface PlanApprovalActionsProps {
  onApprove: (option?: ApprovalOption) => void
  onRequestChanges: (feedback: string) => void
  /**
   * Plan-accept options from the backend registry. When present, one button is
   * rendered per option (Approve (YOLO) / Approve (Act)); when empty/absent the
   * surface degrades to a single generic Approve so older payloads still work.
   */
  options?: ApprovalOption[]
  approveLabel?: string
  rejectLabel?: string
  className?: string
  /**
   * When set, the interactive controls expose `data-testid` hooks
   * (`<prefix>-approve` or `<prefix>-option-<id>`, `<prefix>-reject`,
   * `<prefix>-feedback`, `<prefix>-send`) so each surface can target its own
   * affordances.
   */
  testIdPrefix?: string
  /**
   * Button arrangement. `inline` (default) is one wrapping row, used by the
   * desktop status-bar strip. `stacked` lays the approve options out 2-up
   * (Approve (YOLO) | Approve (Act)) with a full-width Reject below and 44px
   * touch targets — used by the mobile Plans panel.
   */
  layout?: 'inline' | 'stacked'
}

/**
 * The shared approve / reject interaction for a pending plan.
 *
 * Owns only the buttons + comment editor — no surface chrome — so each approval
 * surface can wrap it in its own container while sending the same WS actions.
 * With the backend option set it renders one button per option; without one it
 * falls back to a single Approve. Approval executes on every CLI (managed CLIs
 * auto-continue via the backend), so there is no manual-continue hint.
 *
 * Per .impeccable.md, state is read by icon + position + fill, never hue alone.
 * The hierarchy mirrors the bottom Plan|Act|YOLO control: Approve (YOLO) is the
 * one dominant solid-accent primary, Approve (Act) is the quieter tinted accent,
 * and Reject is the quiet-destructive action (magenta text) that opens an
 * optional comment. YOLO is the comfortable default because the rules engine and
 * the sandbox are the guardrails.
 */
export function PlanApprovalActions({
  onApprove,
  onRequestChanges,
  options,
  approveLabel = 'Approve',
  rejectLabel = 'Reject',
  className,
  testIdPrefix,
  layout = 'inline',
}: PlanApprovalActionsProps) {
  const [showFeedback, setShowFeedback] = useState(false)
  const [feedback, setFeedback] = useState('')

  const stacked = layout === 'stacked'
  const tid = (suffix: string) => (testIdPrefix ? `${testIdPrefix}-${suffix}` : undefined)

  // The comment is optional: an empty submit still rejects the plan.
  const submitReject = () => {
    onRequestChanges(feedback.trim())
    setFeedback('')
    setShowFeedback(false)
  }

  if (showFeedback) {
    return (
      <div className={cn('flex flex-col gap-2', className)}>
        <textarea
          className="min-h-[60px] w-full resize-none rounded-lg bg-muted px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-accent"
          placeholder="Add a comment (optional)…"
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submitReject()
            }
          }}
          data-testid={tid('feedback')}
          autoFocus
          rows={2}
        />
        <div className={stacked ? 'grid grid-cols-2 gap-2' : 'flex gap-2'}>
          <Button
            size="sm"
            variant="destructive"
            className={cn('gap-1.5', stacked && 'w-full pointer-coarse:min-h-11')}
            onClick={submitReject}
            data-testid={tid('send')}
          >
            <RejectIcon />
            {rejectLabel}
          </Button>
          <Button
            size="sm"
            variant="outline"
            className={cn(stacked && 'w-full pointer-coarse:min-h-11')}
            onClick={() => {
              setShowFeedback(false)
              setFeedback('')
            }}
          >
            Cancel
          </Button>
        </div>
      </div>
    )
  }

  const hasOptions = Array.isArray(options) && options.length > 0
  const approveBtnClass = cn('gap-1.5', stacked && 'w-full pointer-coarse:min-h-11')

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className={stacked ? 'grid grid-cols-2 gap-2' : 'flex flex-wrap gap-2'}>
        {hasOptions ? (
          options.map((option) => (
            <Button
              key={option.id}
              size="sm"
              variant={option.emphasis === 'primary' ? 'primary' : 'accent'}
              className={approveBtnClass}
              onClick={() => onApprove(option)}
              title={option.description}
              data-testid={tid(`option-${option.id}`)}
            >
              <CheckIcon />
              {option.label}
            </Button>
          ))
        ) : (
          <Button
            size="sm"
            variant="primary"
            className={cn(approveBtnClass, stacked && 'col-span-2')}
            onClick={() => onApprove()}
            data-testid={tid('approve')}
          >
            <CheckIcon />
            {approveLabel}
          </Button>
        )}
        {/* Inline: Reject shares the wrapping row. Stacked: it drops to its own
            full-width row below the 2-up approve grid (rendered next). */}
        {!stacked && (
          <Button
            size="sm"
            variant="destructive"
            className="gap-1.5"
            onClick={() => setShowFeedback(true)}
            data-testid={tid('reject')}
          >
            <RejectIcon />
            {rejectLabel}
          </Button>
        )}
      </div>
      {stacked && (
        <Button
          size="sm"
          variant="destructive"
          className="w-full gap-1.5 pointer-coarse:min-h-11"
          onClick={() => setShowFeedback(true)}
          data-testid={tid('reject')}
        >
          <RejectIcon />
          {rejectLabel}
        </Button>
      )}
    </div>
  )
}

function CheckIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="shrink-0"
    >
      <path d="M20 6 9 17l-5-5" />
    </svg>
  )
}

function RejectIcon() {
  // Circle-slash: a clear "reject" affordance that reads in grayscale, so the
  // action never leans on the magenta hue alone.
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className="shrink-0"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="m5.6 5.6 12.8 12.8" />
    </svg>
  )
}
