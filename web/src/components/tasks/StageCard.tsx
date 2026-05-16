import { useEffect, useRef, useState } from 'react'
import { draggable } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import {
  resolveAdvanceAction,
  type LifecycleTask,
  type ReviewPolicy,
  type StageAdvanceAction,
  type StageRowState,
} from '../../lib/stageActions'
import { cn } from '../../lib/utils'
import { Button } from '../shared/Button'
import { lifecycleBoardStyles } from './lifecycleBoardStyles'
import type { StageRegistryEntry } from './StageColumn'

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
  onMoveTaskToStage?: (taskId: string, targetStageName: string) => void | Promise<void>
  availableStages?: ReadonlyArray<StageRegistryEntry>
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
  onMoveTaskToStage,
  availableStages = [],
}: StageCardProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [tooltip, setTooltip] = useState<string | null>(null)
  const isBlocked = Boolean(task.is_blocked ?? task.state?.is_blocked)
  const action = resolveAdvanceAction(state, reviewPolicy)
  const disabledReason = isBlocked ? blockedReason(task) : null
  const canMove = Boolean(onMoveTaskToStage && availableStages.length > 1)
  const canAdvance = Boolean(action && onAdvanceStage && !isBlocked)

  useEffect(() => {
    const el = ref.current
    if (!el || !canMove) return
    return draggable({
      element: el,
      getInitialData: () => ({
        type: 'lifecycle-stage-card',
        taskId: task.id,
        sourceStageName: stageName,
        state,
        reviewPolicy,
      }),
      onDragStart: () => setIsDragging(true),
      onDrop: () => setIsDragging(false),
    })
  }, [canMove, reviewPolicy, stageName, state, task.id])

  const showTooltip = (message: string) => {
    setTooltip(message)
  }

  const runAction = (callback: () => void | Promise<void>) => {
    try {
      const result = callback()
      if (result && typeof result === 'object' && 'catch' in result) {
        ;(result as Promise<void>).catch(error => showTooltip(transitionReason(error)))
      }
    } catch (error) {
      showTooltip(transitionReason(error))
    }
  }

  const advance = () => {
    if (isBlocked) {
      showTooltip(blockedReason(task))
      return
    }
    if (!action || !onAdvanceStage) return

    runAction(() => onAdvanceStage(task.id, stageName, action))
  }

  const moveToStage = (targetStageName: string) => {
    if (!onMoveTaskToStage || targetStageName === stageName) return
    runAction(() => onMoveTaskToStage(task.id, targetStageName))
  }

  return (
    <article
      ref={ref}
      className={cn(
        lifecycleBoardStyles.card,
        isDragging && lifecycleBoardStyles.cardDragging,
        isBlocked && lifecycleBoardStyles.cardBlocked,
      )}
      data-testid={`lifecycle-card-${task.id}`}
      data-task-id={task.id}
      draggable={canMove || undefined}
      onMouseEnter={() => {
        if (disabledReason) setTooltip(disabledReason)
      }}
      onMouseLeave={() => {
        if (disabledReason) setTooltip(null)
      }}
    >
      <span className={lifecycleBoardStyles.cardTopline}>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={lifecycleBoardStyles.cardOpenButton}
          onClick={() => onSelectTask(task.id)}
        >
          <span className={lifecycleBoardStyles.cardTitle}>{task.title}</span>
        </Button>
        {isBlocked && (
          <span
            className={lifecycleBoardStyles.blockedBadge}
            aria-label="Blocked"
            onMouseEnter={() => showTooltip(blockedReason(task))}
          >
            Blocked
          </span>
        )}
      </span>
      <span className={lifecycleBoardStyles.cardMeta}>
        <span>{task.ref ?? task.id}</span>
        <span>{task.task_type}</span>
        <span>{state.replace(/_/g, ' ')}</span>
      </span>
      <span className={lifecycleBoardStyles.cardActions}>
        <select
          className={lifecycleBoardStyles.moveSelect}
          value={stageName}
          aria-label={`Move ${task.title} to stage`}
          title="Move to stage"
          disabled={!onMoveTaskToStage || availableStages.length === 0}
          onChange={event => moveToStage(event.currentTarget.value)}
        >
          {availableStages.map(stage => (
            <option key={stage.name} value={stage.name}>
              {stage.display_name}
            </option>
          ))}
        </select>
        {action && onAdvanceStage && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!canAdvance}
            onClick={advance}
          >
            Advance
          </Button>
        )}
      </span>
      {tooltip && (
        <span className={lifecycleBoardStyles.tooltip} role="tooltip" aria-label={tooltip}>
          {tooltip}
        </span>
      )}
    </article>
  )
}
