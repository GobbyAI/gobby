import { useState, useCallback, useEffect } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { cn } from '../../lib/utils'
import {
  TASK_MODAL_BACKDROP_BASE_CLS,
  TASK_MODAL_CLOSE_BTN_CLS,
  TASK_MODAL_HEADER_CLS,
} from './taskModalStyles'

export interface TaskCreateDefaults {
  taskType?: string
  priority?: number
  parentTaskId?: string
  title?: string
  description?: string
  validationCriteria?: string
  labels?: string[]
}

interface TaskCreateFormProps {
  isOpen: boolean
  tasks: GobbyTask[]
  defaults?: TaskCreateDefaults
  onSubmit: (params: CreateTaskParams) => Promise<unknown>
  onClose: () => void
}

interface CreateTaskParams {
  title: string
  description?: string
  priority?: number
  task_type?: string
  parent_task_id?: string
  labels?: string[]
  validation_criteria?: string
}

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

const PRIORITY_OPTIONS = [
  { value: 0, label: 'Critical' },
  { value: 1, label: 'High' },
  { value: 2, label: 'Medium' },
  { value: 3, label: 'Low' },
  { value: 4, label: 'Backlog' },
]

const BACKDROP_CLS = cn(TASK_MODAL_BACKDROP_BASE_CLS, 'z-[200]')
const MODAL_CLS =
  'fixed left-1/2 top-1/2 z-[210] max-h-[85vh] w-[520px] max-w-[90vw] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] shadow-[var(--shadow-xl)]'
const TITLE_CLS = 'text-[length:calc(var(--font-size-base)*1.05)] font-semibold'

const FORM_CLS = 'flex flex-col gap-3 px-5 py-4'
const FIELD_CLS = 'flex flex-col gap-1'
const ROW_CLS = 'flex gap-3 [&>div]:flex-1'
const LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.75)] font-medium text-[var(--text-muted)]'
const REQUIRED_CLS = 'text-[var(--color-error)]'
const INPUT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none pointer-coarse:min-h-11'
const TEXTAREA_CLS =
  'min-h-12 resize-y rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none'

const ACTIONS_CLS = 'flex justify-end gap-2 border-t border-[var(--border)] pt-2'
const CANCEL_BTN_CLS =
  'min-w-[100px] cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--border)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'
const SUBMIT_BTN_CLS =
  'min-w-[100px] cursor-pointer rounded-md border border-[var(--accent)] bg-[var(--accent)] px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

export function TaskCreateForm({ isOpen, tasks, defaults, onSubmit, onClose }: TaskCreateFormProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [taskType, setTaskType] = useState('task')
  const [priority, setPriority] = useState(2)
  const [parentTaskId, setParentTaskId] = useState('')
  const [labelsInput, setLabelsInput] = useState('')
  const [validationCriteria, setValidationCriteria] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (isOpen) {
      setTitle(defaults?.title || '')
      setDescription(defaults?.description || '')
      setTaskType(defaults?.taskType || 'task')
      setPriority(defaults?.priority ?? 2)
      setParentTaskId(defaults?.parentTaskId || '')
      setLabelsInput(defaults?.labels?.join(', ') || '')
      setValidationCriteria(defaults?.validationCriteria || '')
    }
  }, [isOpen, defaults])

  const reset = useCallback(() => {
    setTitle('')
    setDescription('')
    setTaskType('task')
    setPriority(2)
    setParentTaskId('')
    setLabelsInput('')
    setValidationCriteria('')
  }, [])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return

    setSubmitting(true)
    const params: CreateTaskParams = {
      title: title.trim(),
      task_type: taskType,
      priority,
    }
    if (description.trim()) params.description = description.trim()
    if (parentTaskId) params.parent_task_id = parentTaskId
    if (labelsInput.trim()) {
      params.labels = labelsInput.split(',').map(l => l.trim()).filter(Boolean)
    }
    if (validationCriteria.trim()) params.validation_criteria = validationCriteria.trim()

    try {
      await onSubmit(params)
      reset()
      onClose()
    } catch (err) {
      console.error('Failed to create task:', err)
    } finally {
      setSubmitting(false)
    }
  }, [title, description, taskType, priority, parentTaskId, labelsInput, validationCriteria, onSubmit, onClose, reset])

  const handleClose = useCallback(() => {
    reset()
    onClose()
  }, [reset, onClose])

  if (!isOpen) return null

  const parentOptions = tasks.filter(t => t.task_type === 'epic' || t.task_type === 'task')

  return (
    <>
      <div className={BACKDROP_CLS} onClick={handleClose} />
      <div className={MODAL_CLS} role="dialog" aria-modal="true" aria-labelledby="task-create-form-title">
        <div className={TASK_MODAL_HEADER_CLS}>
          <h2 id="task-create-form-title" className={TITLE_CLS}>{defaults?.title ? 'Clone Task' : 'New Task'}</h2>
          <button className={TASK_MODAL_CLOSE_BTN_CLS} onClick={handleClose} title="Close">
            <CloseIcon />
          </button>
        </div>

        <form className={FORM_CLS} onSubmit={handleSubmit}>
          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>
              Title <span className={REQUIRED_CLS}>*</span>
            </label>
            <input
              type="text"
              className={INPUT_CLS}
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="Task title..."
              autoFocus
              required
            />
          </div>

          <div className={ROW_CLS}>
            <div className={FIELD_CLS}>
              <label className={LABEL_CLS}>Type</label>
              <select
                className={INPUT_CLS}
                value={taskType}
                onChange={e => setTaskType(e.target.value)}
              >
                {TYPE_OPTIONS.map(t => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div className={FIELD_CLS}>
              <label className={LABEL_CLS}>Priority</label>
              <select
                className={INPUT_CLS}
                value={priority}
                onChange={e => setPriority(Number(e.target.value))}
              >
                {PRIORITY_OPTIONS.map(p => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Parent Task</label>
            <select
              className={INPUT_CLS}
              value={parentTaskId}
              onChange={e => setParentTaskId(e.target.value)}
            >
              <option value="">None</option>
              {parentOptions.map(t => (
                <option key={t.id} value={t.id}>{t.ref} - {t.title}</option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Description</label>
            <textarea
              className={TEXTAREA_CLS}
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Detailed description..."
              rows={4}
            />
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Labels</label>
            <input
              type="text"
              className={INPUT_CLS}
              value={labelsInput}
              onChange={e => setLabelsInput(e.target.value)}
              placeholder="Comma-separated labels..."
            />
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Validation Criteria</label>
            <textarea
              className={TEXTAREA_CLS}
              value={validationCriteria}
              onChange={e => setValidationCriteria(e.target.value)}
              placeholder="How to verify this task is complete..."
              rows={2}
            />
          </div>

          <div className={ACTIONS_CLS}>
            <button
              type="button"
              className={CANCEL_BTN_CLS}
              onClick={handleClose}
            >
              Cancel
            </button>
            <button
              type="submit"
              className={SUBMIT_BTN_CLS}
              disabled={!title.trim() || submitting}
            >
              {submitting ? 'Creating...' : 'Create Task'}
            </button>
          </div>
        </form>
      </div>
    </>
  )
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}
