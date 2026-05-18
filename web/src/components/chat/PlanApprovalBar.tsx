import { useState } from 'react'
import { Button } from '../shared/Button'

interface PlanApprovalBarProps {
  onApprove: () => void
  onRequestChanges: (feedback: string) => void
}

export function PlanApprovalBar({ onApprove, onRequestChanges }: PlanApprovalBarProps) {
  const [showFeedback, setShowFeedback] = useState(false)
  const [feedback, setFeedback] = useState('')

  return (
    <div className="px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="rounded-lg border border-accent/20 bg-accent/5 p-3">
          <p className="text-sm text-muted-foreground mb-3">
            The agent has presented a plan. Review it above, then approve or request changes.
          </p>
          {showFeedback ? (
            <div className="space-y-2">
              <textarea
                className="w-full bg-muted rounded-lg px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground resize-none focus:outline-none focus:ring-2 focus:ring-accent min-h-[60px]"
                placeholder="Describe what you'd like changed..."
                value={feedback}
                onChange={(e) => setFeedback(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault()
                    if (feedback.trim()) {
                      onRequestChanges(feedback.trim())
                      setFeedback('')
                      setShowFeedback(false)
                    }
                  }
                }}
                autoFocus
                rows={2}
              />
              <div className="flex gap-2">
                <Button
                  size="sm"
                  variant="primary"
                  onClick={() => {
                    if (feedback.trim()) {
                      onRequestChanges(feedback.trim())
                      setFeedback('')
                      setShowFeedback(false)
                    }
                  }}
                  disabled={!feedback.trim()}
                >
                  Send Feedback
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => { setShowFeedback(false); setFeedback('') }}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <Button size="sm" variant="primary" onClick={onApprove}>
                Approve &amp; Execute
              </Button>
              <Button size="sm" variant="outline" onClick={() => setShowFeedback(true)}>
                Request Changes
              </Button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
