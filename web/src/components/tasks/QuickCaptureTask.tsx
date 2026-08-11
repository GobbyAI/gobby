import { useState, useEffect, useRef, useCallback } from 'react'
import { DEFAULT_TASK_PRIORITY } from '../../lib/taskOptions'
import { Button } from '../ui/Button'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { Dialog, DialogContent, DialogTitle } from '../ui/Dialog'
import { Input } from '../ui/Input'

interface QuickCaptureTaskProps {
  isOpen: boolean
  onClose: () => void
}

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

function getBaseUrl(): string {
  return ''
}

export function QuickCaptureTask({ isOpen, onClose }: QuickCaptureTaskProps) {
  const [title, setTitle] = useState('')
  const [taskType, setTaskType] = useState('task')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (isOpen) {
      setTitle('')
      setTaskType('task')
      setError(null)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [isOpen])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim() || submitting) return

    setSubmitting(true)
    setError(null)
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
        setError(`Failed to create task (${response.status})`)
        setSubmitting(false)
        return
      }
    } catch (err) {
      console.error('Failed to create task:', err)
      setError(err instanceof Error ? err.message : 'Failed to create task')
      setSubmitting(false)
      return
    }
    setSubmitting(false)
    onClose()
  }, [title, taskType, submitting, onClose])

  if (!isOpen) return null

  return (
    <Dialog
      open={isOpen}
      onOpenChange={open => {
        if (!open) onClose()
      }}
    >
      <DialogContent
        className="top-[20%] w-[480px] max-w-[90vw] translate-y-0 rounded-[12px] bg-[var(--bg-secondary)] p-4 shadow-[var(--shadow-xl)]"
        aria-label="Quick capture task"
        aria-describedby={undefined}
      >
        <DialogTitle className="sr-only">Quick capture task</DialogTitle>
        <form className="flex flex-col gap-[10px]" onSubmit={handleSubmit}>
          <Input
            ref={inputRef}
            type="text"
            className="box-border h-auto rounded-lg bg-[var(--bg-primary)] py-[10px] font-[var(--font-sans)] text-[length:var(--text-base)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="Task title..."
            required
          />
          <div className="flex items-center justify-between gap-[10px]">
            <div className="flex gap-1">
              {TYPE_OPTIONS.map(t => (
                <Button
                  key={t}
                  type="button"
                  variant={taskType === t ? 'primary' : 'secondary'}
                  size="sm"
                  className={coarseHitAreaCls}
                  onClick={() => setTaskType(t)}
                >
                  {t}
                </Button>
              ))}
            </div>
            <Button
              type="submit"
              variant="primary"
              className={coarseHitAreaCls}
              disabled={!title.trim() || submitting}
            >
              {submitting ? 'Creating...' : 'Create'}
            </Button>
          </div>
          {error && (
            <p className="text-[length:var(--text-sm)] text-[var(--color-error)]" role="alert">
              {error}
            </p>
          )}
          <div className="text-center text-[length:var(--text-xs)] text-[var(--text-muted)]">
            <kbd className="inline-block rounded-[3px] border border-[var(--border)] bg-[var(--bg-tertiary)] px-[5px] py-[1px] font-[inherit] text-[length:var(--text-2xs)]">Enter</kbd> to create &middot;{' '}
            <kbd className="inline-block rounded-[3px] border border-[var(--border)] bg-[var(--bg-tertiary)] px-[5px] py-[1px] font-[inherit] text-[length:var(--text-2xs)]">Esc</kbd> to cancel
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
