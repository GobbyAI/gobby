import { useState } from 'react'
import { Button } from '../shared/Button'
import { cn } from '../../lib/utils'
import { usePlanCapability } from './PlanCapabilityContext'

interface PlanApprovalActionsProps {
  onApprove: () => void
  onRequestChanges: (feedback: string) => void
  approveLabel?: string
  requestChangesLabel?: string
  className?: string
  /**
   * When set, the interactive controls expose `data-testid` hooks
   * (`<prefix>-approve`, `<prefix>-request-changes`, `<prefix>-feedback`,
   * `<prefix>-send`) so each surface can target its own affordances.
   */
  testIdPrefix?: string
}

/**
 * The shared approve / request-changes interaction for a pending plan.
 *
 * Owns only the buttons + feedback editor — no surface chrome — so the Plans
 * panel card, the inline transcript block, and the status-bar affordance can
 * each wrap it in their own container while sending the same WS actions. When
 * the active CLI cannot auto-switch out of plan mode (PlanCapabilityContext),
 * it also surfaces a "manual continue" hint.
 */
export function PlanApprovalActions({
  onApprove,
  onRequestChanges,
  approveLabel = 'Approve & Execute',
  requestChangesLabel = 'Request Changes',
  className,
  testIdPrefix,
}: PlanApprovalActionsProps) {
  const [showFeedback, setShowFeedback] = useState(false)
  const [feedback, setFeedback] = useState('')
  const { manualSwitchRequired } = usePlanCapability()

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

  return (
    <div className={cn('flex flex-col gap-2', className)}>
      {manualSwitchRequired && (
        <p
          data-testid="plan-manual-switch-note"
          className="flex items-center gap-1.5 text-xs text-muted-foreground"
        >
          <InfoIcon className="shrink-0" />
          This CLI stays in plan mode after approval — send a message to continue.
        </p>
      )}
      <div className="flex gap-2">
        <Button size="sm" variant="primary" onClick={onApprove} data-testid={tid('approve')}>
          {approveLabel}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setShowFeedback(true)}
          data-testid={tid('request-changes')}
        >
          {requestChangesLabel}
        </Button>
      </div>
    </div>
  )
}

function InfoIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <line x1="12" y1="11" x2="12" y2="16" />
      <line x1="12" y1="8" x2="12.01" y2="8" />
    </svg>
  )
}
