import { useState, useEffect, useRef, useCallback } from 'react'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import { DEFAULT_TASK_PRIORITY } from '../../lib/taskOptions'

interface QuickCaptureTaskProps {
  isOpen: boolean
  onClose: () => void
}

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

const BACKDROP_CLS = 'fixed inset-0 z-[1000] bg-[var(--surface-scrim)]'
const MODAL_CLS =
  'fixed left-1/2 top-[20%] z-[1001] w-[480px] max-w-[90vw] -translate-x-1/2 rounded-[12px] border border-[var(--border)] bg-[var(--bg-secondary)] p-4 shadow-[var(--shadow-xl)]'
const FORM_CLS = 'flex flex-col gap-[10px]'
const INPUT_CLS =
  'box-border w-full rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-[10px] font-[var(--font-sans)] text-[length:var(--text-base)] text-[var(--text-primary)] outline-none placeholder:text-[var(--text-muted)] focus:border-[var(--accent)]'
const ROW_CLS = 'flex items-center justify-between gap-[10px]'
const TYPES_CLS = 'flex gap-1'
const TYPE_BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-[10px] py-1 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-[background-color,color,border-color] duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-primary)]'
const TYPE_BTN_ACTIVE_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:border-[var(--accent)] hover:text-[var(--accent-foreground)]'
const SUBMIT_CLS =
  'cursor-pointer rounded-md border-none bg-[var(--accent)] px-4 py-1.5 text-[length:var(--text-md)] font-medium text-[var(--accent-foreground)] transition-opacity duration-150 hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50'
const HINT_CLS = 'text-center text-[length:var(--text-xs)] text-[var(--text-muted)]'
const KBD_CLS =
  'inline-block rounded-[3px] border border-[var(--border)] bg-[var(--bg-tertiary)] px-[5px] py-[1px] font-[inherit] text-[length:var(--text-2xs)]'

function getBaseUrl(): string {
  return ''
}

export function QuickCaptureTask({ isOpen, onClose }: QuickCaptureTaskProps) {
  const [title, setTitle] = useState('')
  const [taskType, setTaskType] = useState('task')
  const [submitting, setSubmitting] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus({ ref: dialogRef, isOpen, onClose })

  useEffect(() => {
    if (isOpen) {
      setTitle('')
      setTaskType('task')
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [isOpen])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || submitting) return

    setSubmitting(true)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: title.trim(),
          task_type: taskType,
          priority: DEFAULT_TASK_PRIORITY,
        }),
      })
      if (!response.ok) {
        console.error('Failed to create task:', response.status)
        setSubmitting(false)
        return
      }
    } catch (err) {
      console.error('Failed to create task:', err)
      setSubmitting(false)
      return
    }
    setSubmitting(false)
    onClose()
  }, [title, taskType, submitting, onClose])

  if (!isOpen) return null

  return (
    <div className={BACKDROP_CLS} onClick={onClose}>
      <div
        ref={dialogRef}
        className={MODAL_CLS}
        role="dialog"
        aria-modal="true"
        aria-label="Quick capture task"
        tabIndex={-1}
        onClick={e => e.stopPropagation()}
      >
        <form className={FORM_CLS} onSubmit={handleSubmit}>
          <input
            ref={inputRef}
            type="text"
            className={INPUT_CLS}
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="Task title..."
            required
          />
          <div className={ROW_CLS}>
            <div className={TYPES_CLS}>
              {TYPE_OPTIONS.map(t => (
                <button
                  key={t}
                  type="button"
                  className={taskType === t ? `${TYPE_BTN_CLS} ${TYPE_BTN_ACTIVE_CLS}` : TYPE_BTN_CLS}
                  onClick={() => setTaskType(t)}
                >
                  {t}
                </button>
              ))}
            </div>
            <button
              type="submit"
              className={SUBMIT_CLS}
              disabled={!title.trim() || submitting}
            >
              {submitting ? 'Creating...' : 'Create'}
            </button>
          </div>
          <div className={HINT_CLS}>
            <kbd className={KBD_CLS}>Enter</kbd> to create &middot; <kbd className={KBD_CLS}>Esc</kbd> to cancel
          </div>
        </form>
      </div>
    </div>
  )
}
