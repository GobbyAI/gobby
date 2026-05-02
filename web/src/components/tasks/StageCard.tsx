import { useEffect, useRef, useState } from 'react'
import { draggable } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import {
  resolveAdvanceAction,
  type LifecycleTask,
  type ReviewPolicy,
  type StageAdvanceAction,
  type StageRowState,
} from '../../lib/stageActions'

interface StageCardProps {
  task: LifecycleTask
  stageName: string
  state: StageRowState
  reviewPolicy: ReviewPolicy
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
}

function blockedReason(task: LifecycleTask): string {
  return (
    task.blocked_reason ??
    task.state?.escalation_reason ??
    task.escalation_reason ??
    'Blocked by an open dependency'
  )
}

function transitionReason(error: unknown): string {
  if (typeof error === 'object' && error !== null && 'reason' in error) {
    const reason = (error as { reason?: unknown }).reason
    if (typeof reason === 'string' && reason.trim()) return reason
  }
  if (error instanceof Error && error.message.trim()) return error.message
  return 'Stage transition is not allowed'
}

export function StageCard({
  task,
  stageName,
  state,
  reviewPolicy,
  onSelectTask,
  onAdvanceStage,
}: StageCardProps) {
  const ref = useRef<HTMLButtonElement | null>(null)
  const dragStartX = useRef<number | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [tooltip, setTooltip] = useState<string | null>(null)
  const isBlocked = Boolean(task.is_blocked ?? task.state?.is_blocked)
  const action = resolveAdvanceAction(state, reviewPolicy)
  const disabledReason = isBlocked ? blockedReason(task) : null
  const isDisabled = isBlocked || action === null || !onAdvanceStage

  useEffect(() => {
    const el = ref.current
    if (!el || isDisabled) return
    return draggable({
      element: el,
      getInitialData: () => ({
        type: 'lifecycle-stage-card',
        taskId: task.id,
        stageName,
        state,
        reviewPolicy,
      }),
      onDragStart: () => setIsDragging(true),
      onDrop: () => setIsDragging(false),
    })
  }, [action, isDisabled, reviewPolicy, stageName, state, task.id])

  const showTooltip = (message: string) => {
    setTooltip(message)
  }

  const advance = () => {
    if (isBlocked) {
      showTooltip(blockedReason(task))
      return
    }
    if (!action || !onAdvanceStage) return

    try {
      const result = onAdvanceStage(task.id, stageName, action)
      if (result && typeof result === 'object' && 'catch' in result) {
        ;(result as Promise<void>).catch(error => showTooltip(transitionReason(error)))
      }
    } catch (error) {
      showTooltip(transitionReason(error))
    }
  }

  return (
    <button
      ref={ref}
      type="button"
      className={[
        'lifecycle-card',
        isDragging ? 'lifecycle-card--dragging' : '',
        isBlocked ? 'lifecycle-card--blocked' : '',
      ].filter(Boolean).join(' ')}
      aria-disabled={isDisabled ? 'true' : 'false'}
      onClick={() => onSelectTask(task.id)}
      onPointerDown={event => {
        dragStartX.current = event.clientX
        setTooltip(null)
      }}
      onPointerUp={event => {
        const startX = dragStartX.current
        dragStartX.current = null
        if (startX === null) return
        if (event.clientX - startX >= 80) {
          event.preventDefault()
          advance()
        }
      }}
      onMouseEnter={() => {
        if (disabledReason) setTooltip(disabledReason)
      }}
      onMouseLeave={() => {
        if (disabledReason) setTooltip(null)
      }}
    >
      <span className="lifecycle-card__topline">
        <span className="lifecycle-card__title">{task.title}</span>
        {isBlocked && (
          <span
            className="lifecycle-card__blocked-badge"
            aria-label="Blocked"
            onMouseEnter={() => showTooltip(blockedReason(task))}
          >
            Blocked
          </span>
        )}
      </span>
      <span className="lifecycle-card__meta">
        <span>{task.ref ?? task.id}</span>
        <span>{task.task_type}</span>
        <span>{state.replace(/_/g, ' ')}</span>
      </span>
      {tooltip && (
        <span className="lifecycle-card__tooltip" role="tooltip" aria-label={tooltip}>
          {tooltip}
        </span>
      )}
    </button>
  )
}
