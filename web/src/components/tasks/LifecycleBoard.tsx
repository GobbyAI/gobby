import { useEffect, useMemo, useState } from 'react'
import '../../styles/lifecycle-board.css'
import {
  type LifecycleTask,
  type StageAdvanceAction,
} from '../../lib/stageActions'
import { StageColumn, type StageRegistryEntry } from './StageColumn'
import {
  activeStageCategories,
  buildStageRegistry,
  filterBlockedTasks,
  groupIntoSwimlanes,
  stageCategories,
  stageNamesForTasks,
  tasksForStage,
  visibleStagesForTasks,
} from './lifecycleBoardModel'

interface LifecycleBoardProps {
  tasks: LifecycleTask[]
  registry?: ReadonlyArray<StageRegistryEntry>
  stagesRegistry?: ReadonlyArray<StageRegistryEntry>
  onSelectTask: (id: string) => void
  onAdvanceStage?: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
  ) => void | Promise<void>
  onFailStage?: (taskId: string, stageName: string, reason: string) => void | Promise<void>
}

const HIDE_BLOCKED_KEY = 'lifecycle-board:hide-blocked'

export function LifecycleBoard({
  tasks,
  registry,
  stagesRegistry,
  onSelectTask,
  onAdvanceStage,
}: LifecycleBoardProps) {
  const [hideBlocked, setHideBlocked] = useState(() => {
    if (typeof window === 'undefined') return false
    return window.localStorage.getItem(HIDE_BLOCKED_KEY) === 'true'
  })
  const [selectedCategories, setSelectedCategories] = useState<Set<string> | null>(null)

  const allStages = useMemo(() => {
    const supplied = stagesRegistry ?? registry
    return buildStageRegistry(supplied, tasks)
  }, [registry, stagesRegistry, tasks])

  const visibleTasks = useMemo(
    () => filterBlockedTasks(tasks, hideBlocked),
    [hideBlocked, tasks],
  )
  const categories = useMemo(
    () => stageCategories(allStages),
    [allStages],
  )
  const activeCategories = useMemo(
    () => activeStageCategories(categories, selectedCategories),
    [categories, selectedCategories],
  )
  const visibleStages = useMemo(
    () => visibleStagesForTasks(allStages, visibleTasks, activeCategories),
    [activeCategories, allStages, visibleTasks],
  )
  const swimlanes = useMemo(() => groupIntoSwimlanes(visibleTasks), [visibleTasks])

  useEffect(() => {
    window.localStorage.setItem(HIDE_BLOCKED_KEY, hideBlocked ? 'true' : 'false')
  }, [hideBlocked])

  return (
    <section className="lifecycle-board" role="region" aria-label="Lifecycle board">
      <div className="lifecycle-board__toolbar">
        <label className="lifecycle-board__switch">
          <input
            type="checkbox"
            role="switch"
            checked={hideBlocked}
            onChange={event => setHideBlocked(event.currentTarget.checked)}
          />
          <span>Hide blocked</span>
        </label>
        <div className="lifecycle-board__categories" aria-label="Stage categories">
          {categories.map(category => (
            <label key={category} className="lifecycle-board__category">
              <input
                type="checkbox"
                checked={activeCategories.has(category)}
                onChange={event => {
                  const checked = event.currentTarget.checked
                  setSelectedCategories(previous => {
                    const next = new Set(previous ?? categories)
                    if (checked) next.add(category)
                    else next.delete(category)
                    return next
                  })
                }}
              />
              <span>{category}</span>
            </label>
          ))}
        </div>
      </div>
      <div className="lifecycle-board__lanes">
        {swimlanes.map(lane => {
          const laneStageNames = stageNamesForTasks(lane.tasks)
          const laneStages = visibleStages.filter(stage => laneStageNames.has(stage.name))
          if (!laneStages.length) return null

          return (
            <section
              key={lane.key}
              className="lifecycle-board__lane"
              role="rowgroup"
              aria-label={lane.label}
            >
              <header className="lifecycle-board__lane-header">
                <span>{lane.label}</span>
                <span>{lane.tasks.length}</span>
              </header>
              <div className="lifecycle-board__columns">
                {laneStages.map(stage => (
                  <StageColumn
                    key={stage.name}
                    stage={stage}
                    tasks={tasksForStage(lane.tasks, stage.name)}
                    onSelectTask={onSelectTask}
                    onAdvanceStage={onAdvanceStage}
                    showDoneCardsWhenCollapsed
                  />
                ))}
              </div>
            </section>
          )
        })}
      </div>
    </section>
  )
}
