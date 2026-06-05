import { useState } from 'react'
import type { ApprovalOption } from '../../types/chat'
import { Button } from '../shared/Button'
import { cn } from '../../lib/utils'

interface PlanApprovalActionsProps {
  onApprove: (option?: ApprovalOption) => void
  onRequestChanges: (feedback: string) => void
  /**
   * Per-CLI plan-accept options from the backend registry. When present, one
   * button is rendered per option; when empty/absent the surface degrades to a
   * single generic Approve so older payloads still work.
   */
  options?: ApprovalOption[]
  approveLabel?: string
  requestChangesLabel?: string
  className?: string
  /**
   * When set, the interactive controls expose `data-testid` hooks
   * (`<prefix>-approve` or `<prefix>-option-<id>`, `<prefix>-request-changes`,
   * `<prefix>-feedback`, `<prefix>-send`) so each surface can target its own
   * affordances.
   */
  testIdPrefix?: string
}

/**
 * The shared approve / request-changes interaction for a pending plan.
 *
 * Owns only the buttons + feedback editor — no surface chrome — so each
 * approval surface can wrap it in its own container while sending the same WS
 * actions. With a per-CLI option set it renders one button per option (each
 * CLI's real ExitPlanMode choices); without one it falls back to a single
 * Approve. Approval executes on every CLI (managed CLIs auto-continue via the
 * backend), so there is no manual-continue hint.
 *
 * Per .impeccable.md, state is read by icon + position + fill, never hue
 * alone: approve options are the brand-accent primary with a check; a
 * keep-planning option and Request Changes are neutral outlines with their own
 * icons (never the destructive palette — request-changes is not destructive).
 */
export function PlanApprovalActions({
  onApprove,
  onRequestChanges,
  options,
  approveLabel = 'Approve & Execute',
  requestChangesLabel = 'Request Changes',
  className,
  testIdPrefix,
}: PlanApprovalActionsProps) {
  const [showFeedback, setShowFeedback] = useState(false)
  const [feedback, setFeedback] = useState('')

  const tid = (suffix: string) => (testIdPrefix ? `${testIdPrefix}-${suffix}` : undefined)

  const submitFeedback = () => {
    const trimmed = feedback.trim()
    if (!trimmed) return
    onRequestChanges(trimmed)
    setFeedback('')
    setShowFeedback(false)
  }

  if (showFeedback) {
    return (
      <div className={cn('flex flex-col gap-2', className)}>
        <textarea
          className="min-h-[60px] w-full resize-none rounded-lg bg-muted px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-accent"
          placeholder="Describe what you'd like changed..."
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              submitFeedback()
            }
          }}
          data-testid={tid('feedback')}
          autoFocus
          rows={2}
        />
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="primary"
            onClick={submitFeedback}
            disabled={!feedback.trim()}
            data-testid={tid('send')}
          >
            Send Feedback
          </Button>
          <Button
            size="sm"
            variant="outline"
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

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className="flex flex-wrap gap-2">
        {hasOptions ? (
          options.map((option) => {
            const keepPlanning = option.decision === 'keep_planning'
            return (
              <Button
                key={option.id}
                size="sm"
                variant={keepPlanning ? 'outline' : 'primary'}
                className="gap-1.5"
                onClick={() => onApprove(option)}
                title={option.description}
                data-testid={tid(`option-${option.id}`)}
              >
                {keepPlanning ? <RefineIcon /> : <CheckIcon />}
                {option.label}
              </Button>
            )
          })
        ) : (
          <Button
            size="sm"
            variant="primary"
            className="gap-1.5"
            onClick={() => onApprove()}
            data-testid={tid('approve')}
          >
            <CheckIcon />
            {approveLabel}
          </Button>
        )}
        <Button
          size="sm"
          variant="outline"
          className="gap-1.5"
          onClick={() => setShowFeedback(true)}
          data-testid={tid('request-changes')}
        >
          <EditIcon />
          {requestChangesLabel}
        </Button>
      </div>
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

function RefineIcon() {
  // Sparkle: signals "re-plan deeper" without implying approval.
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
      <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2 2M16 16l2 2M18 6l-2 2M8 16l-2 2" />
    </svg>
  )
}

function EditIcon() {
  // Pencil: "request changes" is an edit affordance, not a destructive action.
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
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z" />
    </svg>
  )
}
