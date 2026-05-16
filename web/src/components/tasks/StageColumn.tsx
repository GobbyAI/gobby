import { useEffect, useMemo, useRef, useState } from 'react'
import { dropTargetForElements } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import {
  type LifecycleTask,
  type ReviewPolicy,
  type StageAdvanceAction,
  type StageRowState,
  type StageStateView,
} from '../../lib/stageActions'
import { cn } from '../../lib/utils'
import { lifecycleBoardStyles, lifecycleGroupStateStyles } from './lifecycleBoardStyles'
import { StageCard } from './StageCard'

export interface StageRegistryEntry {
  name: string
  display_name: string
  category: string
  review_policy: ReviewPolicy | string
  sequence_order?: number | null
}

interface StageColumnProps {
  stage: StageRegistryEntry
  tasks: LifecycleTask[]
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
  onMoveTaskToStage?: (taskId: string, targetStageName: string) => void | Promise<void>
  availableStages?: ReadonlyArray<StageRegistryEntry>
  showDoneCardsWhenCollapsed?: boolean
}

const FULL_STATE_ORDER: StageRowState[] = [
  'ready',
  'in_progress',
  'needs_review',
  'review_approved',
  'done',
]
const SIMPLE_STATE_ORDER: StageRowState[] = ['ready', 'in_progress', 'done']

const GROUP_LABELS: Record<StageRowState, string> = {
  ready: 'Ready',
  in_progress: 'In progress',
  needs_review: 'Needs review',
  review_approved: 'Review approved',
  done: 'Done',
}

function rowForStage(task: LifecycleTask, stageName: string): StageStateView | undefined {
  const rows = task.stages ?? []
  return rows.find(row => row.name === stageName) ?? (rows.length === 1 ? rows[0] : undefined)
}

function stateOrderForPolicy(policy: ReviewPolicy | string): StageRowState[] {
  return policy === 'none' ? SIMPLE_STATE_ORDER : FULL_STATE_ORDER
}

export function StageColumn({
  stage,
  tasks,
  onSelectTask,
  onAdvanceStage,
  onMoveTaskToStage,
  availableStages = [],
  showDoneCardsWhenCollapsed = false,
}: StageColumnProps) {
  const ref = useRef<HTMLElement | null>(null)
  const [showDone, setShowDone] = useState(false)
  const [isOver, setIsOver] = useState(false)
  const stateOrder = stateOrderForPolicy(stage.review_policy)
  const grouped = useMemo(() => {
    const groups = new Map<StageRowState, Array<{ task: LifecycleTask; row: StageStateView }>>()
    for (const state of stateOrder) groups.set(state, [])

    for (const task of tasks) {
      const row = rowForStage(task, stage.name)
      if (!row || !groups.has(row.state)) continue
      groups.get(row.state)!.push({ task, row })
    }
    return groups
  }, [stage.name, stateOrder, tasks])

  const taskCount = Array.from(grouped.values()).reduce((count, rows) => count + rows.length, 0)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    return dropTargetForElements({
      element,
      getData: () => ({
        type: 'lifecycle-stage-column',
        stageName: stage.name,
      }),
      canDrop: ({ source }) => source.data.type === 'lifecycle-stage-card',
      onDragEnter: () => setIsOver(true),
      onDragLeave: () => setIsOver(false),
      onDrop: () => setIsOver(false),
    })
  }, [stage.name])

  return (
    <section
      ref={ref}
      className={cn(lifecycleBoardStyles.column, isOver && lifecycleBoardStyles.columnOver)}
      role="region"
      aria-label={stage.display_name}
      data-testid={`stage-column-${stage.name}`}
      data-stage-name={stage.name}
    >
      <header className={lifecycleBoardStyles.columnHeader}>
        <span className={lifecycleBoardStyles.columnTitle}>{stage.display_name}</span>
        <span className={lifecycleBoardStyles.columnCategory}>{stage.category}</span>
        <span className={lifecycleBoardStyles.columnCount}>{taskCount}</span>
      </header>
      <div className={lifecycleBoardStyles.groups}>
        {stateOrder.map(state => {
          const rows = grouped.get(state) ?? []
          const isDone = state === 'done'
          return (
            <section
              key={state}
              className={cn(lifecycleBoardStyles.group, lifecycleGroupStateStyles[state])}
              data-testid={`stage-group-${stage.name}-${state}`}
              data-state={state}
            >
              {isDone ? (
                <>
                  <button
                    type="button"
                    className={lifecycleBoardStyles.groupSummary}
                    onClick={() => setShowDone(open => !open)}
                  >
                    {GROUP_LABELS[state]} ({rows.length})
                  </button>
                  {(showDone || showDoneCardsWhenCollapsed) && (
                    <div className={lifecycleBoardStyles.cards}>
                      {rows.map(({ task, row }) => (
                        <StageCard
                          key={task.id}
                          task={task}
                          stageName={row.name}
                          state={row.state}
                          reviewPolicy={row.review_policy}
                          onSelectTask={onSelectTask}
                          onAdvanceStage={onAdvanceStage}
                          onMoveTaskToStage={onMoveTaskToStage}
                          availableStages={availableStages}
                        />
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className={lifecycleBoardStyles.groupHeading}>
                    <span>{GROUP_LABELS[state]}</span>
                    <span>{rows.length}</span>
                  </div>
                  <div className={lifecycleBoardStyles.cards}>
                    {rows.map(({ task, row }) => (
                      <StageCard
                        key={task.id}
                        task={task}
                        stageName={row.name}
                        state={row.state}
                        reviewPolicy={row.review_policy}
                        onSelectTask={onSelectTask}
                        onAdvanceStage={onAdvanceStage}
                        onMoveTaskToStage={onMoveTaskToStage}
                        availableStages={availableStages}
                      />
                    ))}
                  </div>
                </>
              )}
            </section>
          )
        })}
      </div>
    </section>
  )
}
