import { useState, useCallback } from 'react'

// =============================================================================
// Types
// =============================================================================

type HandoffTarget = 'agent' | 'human'

interface HandoffContext {
  target: HandoffTarget
  assignee: string
  whatsDone: string
  whatsLeft: string
  blockers: string
}

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function formatHandoffComment(ctx: HandoffContext): string {
  const targetLabel = ctx.target === 'agent' ? 'Agent' : 'Human'
  const lines = [`**Handoff to ${targetLabel}**: ${ctx.assignee}`]
  if (ctx.whatsDone.trim()) lines.push(`\n**Completed:**\n${ctx.whatsDone.trim()}`)
  if (ctx.whatsLeft.trim()) lines.push(`\n**Remaining:**\n${ctx.whatsLeft.trim()}`)
  if (ctx.blockers.trim()) lines.push(`\n**Blockers:**\n${ctx.blockers.trim()}`)
  return lines.join('\n')
}

// =============================================================================
// TaskHandoff
// =============================================================================

interface TaskHandoffProps {
  taskId: string
  currentAssignee: string | null
  onHandoff: (assignee: string) => Promise<void>
}

export function TaskHandoff({ taskId, currentAssignee, onHandoff }: TaskHandoffProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [target, setTarget] = useState<HandoffTarget>('agent')
  const [assignee, setAssignee] = useState('')
  const [whatsDone, setWhatsDone] = useState('')
  const [whatsLeft, setWhatsLeft] = useState('')
  const [blockers, setBlockers] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reset = useCallback(() => {
    setTarget('agent')
    setAssignee('')
    setWhatsDone('')
    setWhatsLeft('')
    setBlockers('')
    setError(null)
  }, [])

  const handleSubmit = useCallback(async () => {
    if (!assignee.trim()) return
    setSubmitting(true)
    setError(null)

    const ctx: HandoffContext = {
      target,
      assignee: assignee.trim(),
      whatsDone,
      whatsLeft,
      blockers,
    }

    try {
      // Post handoff comment
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          body: formatHandoffComment(ctx),
          author: 'web-user',
          author_type: 'human',
        }),
      })

      if (!response.ok) {
        throw new Error(`Failed to post handoff comment: ${response.statusText}`)
      }

      // Update assignee
      try {
        await onHandoff(assignee.trim())
      } catch (handoffErr) {
        console.error('Assignee update failed:', handoffErr)
        setError('Comment posted but assignee update failed.')
        return
      }

      reset()
      setIsOpen(false)
    } catch (e) {
      console.error('Handoff failed:', e)
      setError('Failed to post handoff comment. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }, [taskId, target, assignee, whatsDone, whatsLeft, blockers, onHandoff, reset])

  if (!isOpen) {
    return (
      <div className="task-handoff-buttons">
        <button
          className="task-handoff-trigger task-handoff-trigger--agent"
          onClick={() => { setTarget('agent'); setIsOpen(true) }}
          title="Transfer this task to an agent"
        >
          {'\u2699'} Hand to Agent
        </button>
        <button
          className="task-handoff-trigger task-handoff-trigger--human"
          onClick={() => { setTarget('human'); setIsOpen(true) }}
          title="Transfer this task to a human"
        >
          {'\u{1F464}'} Hand to Human
        </button>
      </div>
    )
  }

  return (
    <div className="task-handoff-form">
      <div className="task-handoff-form-header">
        <span className="task-handoff-form-title">
          Handoff to {target === 'agent' ? 'Agent' : 'Human'}
        </span>
        <button
          className="task-handoff-form-close"
          onClick={() => { reset(); setIsOpen(false) }}
          aria-label="Close handoff form"
          type="button"
        >
          {'\u2715'}
        </button>
      </div>

      {/* Target toggle */}
      <div className="task-handoff-target-toggle">
        <button
          className={`task-handoff-target-btn ${target === 'agent' ? 'active' : ''}`}
          onClick={() => setTarget('agent')}
        >
          {'\u2699'} Agent
        </button>
        <button
          className={`task-handoff-target-btn ${target === 'human' ? 'active' : ''}`}
          onClick={() => setTarget('human')}
        >
          {'\u{1F464}'} Human
        </button>
      </div>

      {/* Assignee */}
      <div className="task-handoff-field">
        <label htmlFor="handoff-assignee" className="task-handoff-label">
          New assignee <span className="task-handoff-required">*</span>
        </label>
        <input
          id="handoff-assignee"
          className="task-handoff-input"
          value={assignee}
          onChange={e => setAssignee(e.target.value)}
          placeholder={target === 'agent' ? 'Session ID or agent name...' : 'Name or identifier...'}
          autoFocus
        />
        {currentAssignee && (
          <span className="task-handoff-current">
            Currently: {currentAssignee}
          </span>
        )}
      </div>

      {/* What's done */}
      <div className="task-handoff-field">
        <label htmlFor="handoff-done" className="task-handoff-label">What's been completed</label>
        <textarea
          id="handoff-done"
          className="task-handoff-textarea"
          value={whatsDone}
          onChange={e => setWhatsDone(e.target.value)}
          placeholder="Summary of work completed so far..."
          rows={2}
        />
      </div>

      {/* What's left */}
      <div className="task-handoff-field">
        <label htmlFor="handoff-remaining" className="task-handoff-label">What's remaining</label>
        <textarea
          id="handoff-remaining"
          className="task-handoff-textarea"
          value={whatsLeft}
          onChange={e => setWhatsLeft(e.target.value)}
          placeholder="Next steps and remaining work..."
          rows={2}
        />
      </div>

      {/* Blockers */}
      <div className="task-handoff-field">
        <label htmlFor="handoff-blockers" className="task-handoff-label">Blockers</label>
        <textarea
          id="handoff-blockers"
          className="task-handoff-textarea"
          value={blockers}
          onChange={e => setBlockers(e.target.value)}
          placeholder="Any blockers or issues to be aware of..."
          rows={2}
        />
      </div>

      {error && (
        <div className="task-handoff-error">{error}</div>
      )}

      {/* Actions */}
      <div className="task-handoff-actions">
        <button
          type="button"
          className="task-handoff-cancel"
          onClick={() => { reset(); setIsOpen(false) }}
        >
          Cancel
        </button>
        <button
          type="button"
          className="task-handoff-submit"
          onClick={handleSubmit}
          disabled={!assignee.trim() || submitting}
        >
          {submitting ? 'Handing off...' : `Hand to ${target === 'agent' ? 'Agent' : 'Human'}`}
        </button>
      </div>
    </div>
  )
}
