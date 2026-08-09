import { useState, useCallback, useEffect } from 'react'
import type { GobbyTask } from '../../types/tasks'
import { cn } from '../../lib/utils'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import {
  DEFAULT_TASK_PRIORITY,
  TASK_CATEGORY_OPTIONS,
  TASK_PRIORITY_OPTIONS,
} from '../../lib/taskOptions'
import { Button } from '../ui/Button'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { Dialog, DialogContent, DialogTitle } from '../ui/Dialog'
import { FormField } from '../ui/FormField'
import { Input } from '../ui/Input'
import { NativeSelect } from '../ui/NativeSelect'
import { Textarea } from '../ui/Textarea'

export interface TaskCreateDefaults {
  taskType?: string
  priority?: number
  parentTaskId?: string
  title?: string
  description?: string
  validationCriteria?: string
  labels?: string[]
  category?: string
}

interface TaskCreateFormProps {
  isOpen: boolean
  tasks: GobbyTask[]
  defaults?: TaskCreateDefaults
  onSubmit: (params: CreateTaskParams) => Promise<unknown>
  onClose: () => void
}

export interface CreateTaskParams {
  title: string
  description?: string
  priority?: number
  task_type?: string
  parent_task_id?: string
  labels?: string[]
  category?: string
  validation_criteria?: string
}

const TYPE_OPTIONS = ['task', 'bug', 'feature', 'epic', 'chore']

export function TaskCreateForm({ isOpen, tasks, defaults, onSubmit, onClose }: TaskCreateFormProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [taskType, setTaskType] = useState('task')
  const [priority, setPriority] = useState(DEFAULT_TASK_PRIORITY)
  const [category, setCategory] = useState('')
  const [parentTaskId, setParentTaskId] = useState('')
  const [labelsInput, setLabelsInput] = useState('')
  const [validationCriteria, setValidationCriteria] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const resetAndClose = useCallback(() => {
    setTitle('')
    setDescription('')
    setTaskType('task')
    setPriority(DEFAULT_TASK_PRIORITY)
    setCategory('')
    setParentTaskId('')
    setLabelsInput('')
    setValidationCriteria('')
    setError(null)
    onClose()
  }, [onClose])

  const isDirty =
    title !== (defaults?.title || '') ||
    description !== (defaults?.description || '') ||
    taskType !== (defaults?.taskType || 'task') ||
    priority !== (defaults?.priority ?? DEFAULT_TASK_PRIORITY) ||
    category !== (defaults?.category || '') ||
    parentTaskId !== (defaults?.parentTaskId || '') ||
    labelsInput !== (defaults?.labels?.join(', ') || '') ||
    validationCriteria !== (defaults?.validationCriteria || '')

  const requestClose = useCallback(async () => {
    if (
      isDirty &&
      !(await confirm({
        title: 'Discard task draft?',
        description: 'Your task draft has unsaved changes.',
        confirmLabel: 'Discard',
        destructive: true,
      }))
    ) {
      return
    }
    resetAndClose()
  }, [confirm, isDirty, resetAndClose])

  useEffect(() => {
    if (isOpen) {
      setTitle(defaults?.title || '')
      setDescription(defaults?.description || '')
      setTaskType(defaults?.taskType || 'task')
      setPriority(defaults?.priority ?? DEFAULT_TASK_PRIORITY)
      setCategory(defaults?.category || '')
      setParentTaskId(defaults?.parentTaskId || '')
      setLabelsInput(defaults?.labels?.join(', ') || '')
      setValidationCriteria(defaults?.validationCriteria || '')
      setError(null)
    }
  }, [isOpen, defaults])

  const handleSubmit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    if (!title.trim()) return

    setSubmitting(true)
    setError(null)
    const params: CreateTaskParams = {
      title: title.trim(),
      task_type: taskType,
      priority,
    }
    if (description.trim()) params.description = description.trim()
    const trimmedCategory = category.trim()
    if (trimmedCategory) params.category = trimmedCategory
    if (parentTaskId) params.parent_task_id = parentTaskId
    if (labelsInput.trim()) {
      params.labels = labelsInput.split(',').map(l => l.trim()).filter(Boolean)
    }
    if (validationCriteria.trim()) params.validation_criteria = validationCriteria.trim()

    try {
      await onSubmit(params)
      resetAndClose()
    } catch (err) {
      console.error('Failed to create task:', err)
      setError(err instanceof Error ? err.message : 'Failed to create task')
    } finally {
      setSubmitting(false)
    }
  }, [title, description, taskType, priority, category, parentTaskId, labelsInput, validationCriteria, onSubmit, resetAndClose])

  if (!isOpen) return null

  const parentOptions = tasks.filter(t => t.task_type === 'epic' || t.task_type === 'task')

  return (
    <>
      <Dialog
        open={isOpen}
        onOpenChange={open => {
          if (!open) void requestClose()
        }}
      >
        <DialogContent
          className="max-h-[85vh] w-[520px] max-w-[90vw] overflow-y-auto rounded-xl bg-[var(--bg-secondary)] p-0 shadow-[var(--shadow-xl)]"
          aria-describedby={undefined}
        >
          <div className="flex items-center justify-between border-b border-[var(--border)] px-5 py-4">
            <DialogTitle>
              {defaults?.title ? 'Clone Task' : 'New Task'}
            </DialogTitle>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={coarseHitAreaCls}
              onClick={() => void requestClose()}
              title="Close"
              aria-label="Close"
            >
              <CloseIcon />
            </Button>
          </div>

          <form className="flex flex-col gap-3 px-5 py-4" onSubmit={handleSubmit}>
            <FormField
              label={
                <>
                  Title <span className="text-[var(--color-error)]">*</span>
                </>
              }
            >
              {({ id }) => (
                <Input
                  id={id}
                  type="text"
                  className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  value={title}
                  onChange={e => setTitle(e.target.value)}
                  placeholder="Task title..."
                  autoFocus
                  required
                />
              )}
            </FormField>

            <div className="flex gap-3 [&>*]:flex-1">
              <FormField label="Type">
                {({ id }) => (
                  <NativeSelect
                    id={id}
                    className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)]"
                    value={taskType}
                    onChange={e => setTaskType(e.target.value)}
                  >
                    {TYPE_OPTIONS.map(t => (
                      <option key={t} value={t}>{t}</option>
                    ))}
                  </NativeSelect>
                )}
              </FormField>
              <FormField label="Priority">
                {({ id }) => (
                  <NativeSelect
                    id={id}
                    className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)]"
                    value={priority}
                    onChange={e => setPriority(Number(e.target.value))}
                  >
                    {TASK_PRIORITY_OPTIONS.map(p => (
                      <option key={p.value} value={p.value}>{p.label}</option>
                    ))}
                  </NativeSelect>
                )}
              </FormField>
            </div>

            <FormField label="Category">
              {({ id }) => (
                <NativeSelect
                  id={id}
                  className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)]"
                  value={category}
                  onChange={e => setCategory(e.target.value)}
                >
                  {TASK_CATEGORY_OPTIONS.map(option => (
                    <option key={option.value} value={option.value}>{option.label}</option>
                  ))}
                </NativeSelect>
              )}
            </FormField>

            <FormField label="Parent Task">
              {({ id }) => (
                <NativeSelect
                  id={id}
                  className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)]"
                  value={parentTaskId}
                  onChange={e => setParentTaskId(e.target.value)}
                >
                  <option value="">None</option>
                  {parentOptions.map(t => (
                    <option key={t.id} value={t.id}>{t.ref} - {t.title}</option>
                  ))}
                </NativeSelect>
              )}
            </FormField>

            <FormField label="Description">
              {({ id }) => (
                <Textarea
                  id={id}
                  className="min-h-12 resize-y bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  value={description}
                  onChange={e => setDescription(e.target.value)}
                  placeholder="Detailed description..."
                  rows={4}
                />
              )}
            </FormField>

            <FormField label="Labels">
              {({ id }) => (
                <Input
                  id={id}
                  type="text"
                  className="bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  value={labelsInput}
                  onChange={e => setLabelsInput(e.target.value)}
                  placeholder="Comma-separated labels..."
                />
              )}
            </FormField>

            <FormField label="Validation Criteria">
              {({ id }) => (
                <Textarea
                  id={id}
                  className="min-h-12 resize-y bg-[var(--bg-tertiary)] text-base text-[var(--text-primary)] placeholder:text-[var(--text-muted)]"
                  value={validationCriteria}
                  onChange={e => setValidationCriteria(e.target.value)}
                  placeholder="How to verify this task is complete..."
                  rows={2}
                />
              )}
            </FormField>

            {error && <p className="text-md text-[var(--color-error)]" role="alert">{error}</p>}

            <div className="flex justify-end gap-2 border-t border-[var(--border)] pt-2">
              <Button
                type="button"
                variant="secondary"
                className={cn(coarseHitAreaCls, 'min-w-[100px]')}
                onClick={() => void requestClose()}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                className={cn(coarseHitAreaCls, 'min-w-[100px]')}
                disabled={!title.trim() || submitting}
              >
                {submitting ? 'Creating...' : 'Create Task'}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
      {ConfirmDialogElement}
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
