import { useState, useCallback } from 'react'
import { cn } from '../../lib/utils'

type HandoffTarget = 'agent' | 'human'

interface HandoffContext {
  target: HandoffTarget
  assignee: string
  whatsDone: string
  whatsLeft: string
  blockers: string
}

const BUTTONS_CLS = 'flex gap-1.5'
const TRIGGER_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-transparent px-2.5 py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] transition-colors duration-150 pointer-coarse:min-h-11'
const TRIGGER_AGENT_CLS =
  'text-[var(--accent)] hover:border-[var(--accent)] hover:bg-[color-mix(in_srgb,var(--color-info)_8%,transparent)]'
const TRIGGER_HUMAN_CLS =
  'text-[var(--color-agent)] hover:border-[var(--color-agent)] hover:bg-[color-mix(in_srgb,var(--color-agent)_8%,transparent)]'

const FORM_CLS = 'flex flex-col gap-2 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-2.5'
const FORM_HEADER_CLS = 'flex items-center justify-between'
const FORM_TITLE_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--text-primary)]'
const FORM_CLOSE_CLS =
  'cursor-pointer border-0 bg-transparent px-1 py-0.5 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11'

const TARGET_TOGGLE_CLS = 'flex gap-0.5 rounded-md bg-[var(--bg-primary)] p-0.5'
const TARGET_BTN_CLS =
  'flex-1 cursor-pointer rounded border-0 bg-transparent px-2 py-1 text-[length:calc(var(--font-size-base)*0.68)] font-medium text-[var(--text-muted)] transition-colors duration-150 hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TARGET_BTN_ACTIVE_CLS =
  'bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]'

const FIELD_CLS = 'flex flex-col gap-[3px]'
const LABEL_CLS =
  'text-[length:calc(var(--font-size-base)*0.62)] font-semibold uppercase tracking-[0.03em] text-[var(--text-muted)]'
const REQUIRED_CLS = 'text-[var(--color-error)]'
const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-[5px] font-[inherit] text-[length:calc(var(--font-size-base)*0.72)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] pointer-coarse:min-h-11'
const CURRENT_CLS = 'text-[length:calc(var(--font-size-base)*0.58)] text-[var(--text-muted)]'
const TEXTAREA_CLS =
  'resize-y rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-[5px] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]'

const ERROR_CLS =
  'rounded bg-[var(--color-error-soft)] px-2 py-1.5 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--color-error)]'
const ACTIONS_CLS = 'flex justify-end gap-1.5 pt-0.5'
const CANCEL_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-transparent px-3 py-1 text-[length:calc(var(--font-size-base)*0.68)] text-[var(--text-muted)] hover:border-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const SUBMIT_CLS =
  'cursor-pointer rounded border-0 bg-[var(--accent)] px-3.5 py-1 text-[length:calc(var(--font-size-base)*0.68)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

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
      <div className={BUTTONS_CLS}>
        <button
          className={cn(TRIGGER_CLS, TRIGGER_AGENT_CLS)}
          onClick={() => { setTarget('agent'); setIsOpen(true) }}
          title="Transfer this task to an agent"
        >
          {'⚙'} Hand to Agent
        </button>
        <button
          className={cn(TRIGGER_CLS, TRIGGER_HUMAN_CLS)}
          onClick={() => { setTarget('human'); setIsOpen(true) }}
          title="Transfer this task to a human"
        >
          {'\u{1F464}'} Hand to Human
        </button>
      </div>
    )
  }

  return (
    <div className={FORM_CLS}>
      <div className={FORM_HEADER_CLS}>
        <span className={FORM_TITLE_CLS}>
          Handoff to {target === 'agent' ? 'Agent' : 'Human'}
        </span>
        <button
          className={FORM_CLOSE_CLS}
          onClick={() => { reset(); setIsOpen(false) }}
          aria-label="Close handoff form"
          type="button"
        >
          {'✕'}
        </button>
      </div>

      <div className={TARGET_TOGGLE_CLS}>
        <button
          className={cn(TARGET_BTN_CLS, target === 'agent' && TARGET_BTN_ACTIVE_CLS)}
          onClick={() => setTarget('agent')}
        >
          {'⚙'} Agent
        </button>
        <button
          className={cn(TARGET_BTN_CLS, target === 'human' && TARGET_BTN_ACTIVE_CLS)}
          onClick={() => setTarget('human')}
        >
          {'\u{1F464}'} Human
        </button>
      </div>

      <div className={FIELD_CLS}>
        <label htmlFor="handoff-assignee" className={LABEL_CLS}>
          New assignee <span className={REQUIRED_CLS}>*</span>
        </label>
        <input
          id="handoff-assignee"
          className={INPUT_CLS}
          value={assignee}
          onChange={e => setAssignee(e.target.value)}
          placeholder={target === 'agent' ? 'Session ID or agent name...' : 'Name or identifier...'}
          autoFocus
        />
        {currentAssignee && (
          <span className={CURRENT_CLS}>
            Currently: {currentAssignee}
          </span>
        )}
      </div>

      <div className={FIELD_CLS}>
        <label htmlFor="handoff-done" className={LABEL_CLS}>What's been completed</label>
        <textarea
          id="handoff-done"
          className={TEXTAREA_CLS}
          value={whatsDone}
          onChange={e => setWhatsDone(e.target.value)}
          placeholder="Summary of work completed so far..."
          rows={2}
        />
      </div>

      <div className={FIELD_CLS}>
        <label htmlFor="handoff-remaining" className={LABEL_CLS}>What's remaining</label>
        <textarea
          id="handoff-remaining"
          className={TEXTAREA_CLS}
          value={whatsLeft}
          onChange={e => setWhatsLeft(e.target.value)}
          placeholder="Next steps and remaining work..."
          rows={2}
        />
      </div>

      <div className={FIELD_CLS}>
        <label htmlFor="handoff-blockers" className={LABEL_CLS}>Blockers</label>
        <textarea
          id="handoff-blockers"
          className={TEXTAREA_CLS}
          value={blockers}
          onChange={e => setBlockers(e.target.value)}
          placeholder="Any blockers or issues to be aware of..."
          rows={2}
        />
      </div>

      {error && (
        <div className={ERROR_CLS}>{error}</div>
      )}

      <div className={ACTIONS_CLS}>
        <button
          type="button"
          className={CANCEL_CLS}
          onClick={() => { reset(); setIsOpen(false) }}
        >
          Cancel
        </button>
        <button
          type="button"
          className={SUBMIT_CLS}
          onClick={handleSubmit}
          disabled={!assignee.trim() || submitting}
        >
          {submitting ? 'Handing off...' : `Hand to ${target === 'agent' ? 'Agent' : 'Human'}`}
        </button>
      </div>
    </div>
  )
}
