import { useMemo, useState } from 'react'
import {
  type LifecycleTask,
  type ReviewPolicy,
  type StageAdvanceAction,
  type StageRowState,
  type StageStateView,
} from '../../lib/stageActions'
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
  showDoneCardsWhenCollapsed = false,
}: StageColumnProps) {
  const [showDone, setShowDone] = useState(false)
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

  return (
    <section className="lifecycle-column" role="region" aria-label={stage.display_name}>
      <header className="lifecycle-column__header">
        <span className="lifecycle-column__title">{stage.display_name}</span>
        <span className="lifecycle-column__category">{stage.category}</span>
        <span className="lifecycle-column__count">{taskCount}</span>
      </header>
      <div className="lifecycle-column__groups">
        {stateOrder.map(state => {
          const rows = grouped.get(state) ?? []
          const isDone = state === 'done'
          return (
            <section
              key={state}
              className={`lifecycle-stage-group lifecycle-stage-group--${state}`}
              data-testid={`stage-group-${stage.name}-${state}`}
              data-state={state}
            >
              {isDone ? (
                <>
                  <button
                    type="button"
                    className="lifecycle-stage-group__summary"
                    onClick={() => setShowDone(open => !open)}
                  >
                    {GROUP_LABELS[state]} ({rows.length})
                  </button>
                  {(showDone || showDoneCardsWhenCollapsed) && (
                    <div className="lifecycle-stage-group__cards">
                      {rows.map(({ task, row }) => (
                        <StageCard
                          key={task.id}
                          task={task}
                          stageName={row.name}
                          state={row.state}
                          reviewPolicy={row.review_policy}
                          onSelectTask={onSelectTask}
                          onAdvanceStage={onAdvanceStage}
                        />
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <>
                  <div className="lifecycle-stage-group__heading">
                    <span>{GROUP_LABELS[state]}</span>
                    <span>{rows.length}</span>
                  </div>
                  <div className="lifecycle-stage-group__cards">
                    {rows.map(({ task, row }) => (
                      <StageCard
                        key={task.id}
                        task={task}
                        stageName={row.name}
                        state={row.state}
                        reviewPolicy={row.review_policy}
                        onSelectTask={onSelectTask}
                        onAdvanceStage={onAdvanceStage}
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
