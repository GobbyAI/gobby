import { useState, useCallback, useEffect } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'

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

export function TaskCreateForm({ isOpen, tasks, defaults, onSubmit, onClose }: TaskCreateFormProps) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [taskType, setTaskType] = useState('task')
  const [priority, setPriority] = useState(2)
  const [parentTaskId, setParentTaskId] = useState('')
  const [labelsInput, setLabelsInput] = useState('')
  const [validationCriteria, setValidationCriteria] = useState('')
  const [submitting, setSubmitting] = useState(false)

  // Reset and apply defaults when form opens
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

  // Parent task options: epics and tasks that can be parents
  const parentOptions = tasks.filter(t => t.task_type === 'epic' || t.task_type === 'task')

  return (
    <>
      <div className="task-create-backdrop" onClick={handleClose} />
      <div className="task-create-modal">
        <div className="task-create-header">
          <h3 className="task-create-title">{defaults?.title ? 'Clone Task' : 'New Task'}</h3>
          <button className="task-detail-close" onClick={handleClose} title="Close">
            <CloseIcon />
          </button>
        </div>

        <form className="task-create-form" onSubmit={handleSubmit}>
          {/* Title */}
          <div className="task-create-field">
            <label className="task-create-label">
              Title <span className="task-create-required">*</span>
            </label>
            <input
              type="text"
              className="task-create-input"
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="Task title..."
              autoFocus
              required
            />
          </div>

          {/* Type & Priority row */}
          <div className="task-create-row">
            <div className="task-create-field">
              <label className="task-create-label">Type</label>
              <select
                className="task-create-select"
                value={taskType}
                onChange={e => setTaskType(e.target.value)}
              >
                {TYPE_OPTIONS.map(t => (
                  <option key={t} value={t}>{t}</option>
                ))}
              </select>
            </div>
            <div className="task-create-field">
              <label className="task-create-label">Priority</label>
              <select
                className="task-create-select"
                value={priority}
                onChange={e => setPriority(Number(e.target.value))}
              >
                {PRIORITY_OPTIONS.map(p => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>
          </div>

          {/* Parent task */}
          <div className="task-create-field">
            <label className="task-create-label">Parent Task</label>
            <select
              className="task-create-select"
              value={parentTaskId}
              onChange={e => setParentTaskId(e.target.value)}
            >
              <option value="">None</option>
              {parentOptions.map(t => (
                <option key={t.id} value={t.id}>{t.ref} - {t.title}</option>
              ))}
            </select>
          </div>

          {/* Description */}
          <div className="task-create-field">
            <label className="task-create-label">Description</label>
            <textarea
              className="task-create-textarea"
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Detailed description..."
              rows={4}
            />
          </div>

          {/* Labels */}
          <div className="task-create-field">
            <label className="task-create-label">Labels</label>
            <input
              type="text"
              className="task-create-input"
              value={labelsInput}
              onChange={e => setLabelsInput(e.target.value)}
              placeholder="Comma-separated labels..."
            />
          </div>

          {/* Validation criteria */}
          <div className="task-create-field">
            <label className="task-create-label">Validation Criteria</label>
            <textarea
              className="task-create-textarea"
              value={validationCriteria}
              onChange={e => setValidationCriteria(e.target.value)}
              placeholder="How to verify this task is complete..."
              rows={2}
            />
          </div>

          {/* Actions */}
          <div className="task-create-actions">
            <button
              type="button"
              className="task-detail-action-btn task-detail-action-btn--default"
              onClick={handleClose}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="task-detail-action-btn task-detail-action-btn--primary"
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
