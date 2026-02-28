import { useState, useEffect, useRef, useCallback } from 'react'

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
  const inputRef = useRef<HTMLInputElement>(null)

  // Auto-focus title input when opened
  useEffect(() => {
    if (isOpen) {
      setTitle('')
      setTaskType('task')
      // Delay focus slightly so the element is rendered
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
        body: JSON.stringify({ title: title.trim(), task_type: taskType, priority: 2 }),
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

  // Close on Escape
  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  if (!isOpen) return null

  return (
    <>
      <div className="quick-capture-backdrop" onClick={onClose} />
      <div className="quick-capture-modal">
        <form className="quick-capture-form" onSubmit={handleSubmit}>
          <input
            ref={inputRef}
            type="text"
            className="quick-capture-input"
            value={title}
            onChange={e => setTitle(e.target.value)}
            placeholder="Task title..."
            required
          />
          <div className="quick-capture-row">
            <div className="quick-capture-types">
              {TYPE_OPTIONS.map(t => (
                <button
                  key={t}
                  type="button"
                  className={`quick-capture-type-btn ${taskType === t ? 'active' : ''}`}
                  onClick={() => setTaskType(t)}
                >
                  {t}
                </button>
              ))}
            </div>
            <button
              type="submit"
              className="quick-capture-submit"
              disabled={!title.trim() || submitting}
            >
              {submitting ? 'Creating...' : 'Create'}
            </button>
          </div>
          <div className="quick-capture-hint">
            <kbd>Enter</kbd> to create &middot; <kbd>Esc</kbd> to cancel
          </div>
        </form>
      </div>
    </>
  )
}
