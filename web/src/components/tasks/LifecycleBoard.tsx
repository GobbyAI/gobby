import { useEffect, useMemo, useRef, useState } from 'react'
import { monitorForElements } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import {
  type LifecycleTask,
  type StageAdvanceAction,
} from '../../lib/stageActions'
import { lifecycleBoardStyles } from './lifecycleBoardStyles'
import { LifecycleLane } from './LifecycleLane'
import type { StageRegistryEntry } from './StageColumn'
import {
  activeStageCategories,
  buildStageRegistry,
  filterBlockedTasks,
  groupIntoSwimlanes,
  stageCategories,
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
  onMoveTaskToStage?: (taskId: string, targetStageName: string) => void | Promise<void>
}

const HIDE_BLOCKED_KEY = 'lifecycle-board:hide-blocked'

export function LifecycleBoard({
  tasks,
  registry,
  stagesRegistry,
  onSelectTask,
  onAdvanceStage,
  onMoveTaskToStage,
}: LifecycleBoardProps) {
  const pendingMoves = useRef<Set<string>>(new Set())
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

  useEffect(() => {
    if (!onMoveTaskToStage) return
    return monitorForElements({
      canMonitor: ({ source }) => source.data.type === 'lifecycle-stage-card',
      onDrop: ({ source, location }) => {
        const taskId = typeof source.data.taskId === 'string' ? source.data.taskId : null
        const sourceStageName = typeof source.data.sourceStageName === 'string'
          ? source.data.sourceStageName
          : null
        const target = location.current.dropTargets.find(
          record => record.data.type === 'lifecycle-stage-column',
        )
        const targetStageName = typeof target?.data.stageName === 'string'
          ? target.data.stageName
          : null
        if (!taskId || !sourceStageName || !targetStageName) return
        if (sourceStageName === targetStageName) return
        if (pendingMoves.current.has(taskId)) return
        pendingMoves.current.add(taskId)
        let moveResult: void | Promise<void>
        try {
          moveResult = onMoveTaskToStage(taskId, targetStageName)
        } catch {
          pendingMoves.current.delete(taskId)
          return
        }
        Promise.resolve(moveResult)
          .catch(() => undefined)
          .finally(() => {
            pendingMoves.current.delete(taskId)
          })
      },
    })
  }, [onMoveTaskToStage])

  return (
    <section className={lifecycleBoardStyles.board} role="region" aria-label="Lifecycle board">
      <div className={lifecycleBoardStyles.toolbar}>
        <label className={lifecycleBoardStyles.switch}>
          <input
            type="checkbox"
            role="switch"
            checked={hideBlocked}
            onChange={event => setHideBlocked(event.currentTarget.checked)}
          />
          <span>Hide blocked</span>
        </label>
        <div className={lifecycleBoardStyles.categories} aria-label="Stage categories">
          {categories.map(category => (
            <label key={category} className={lifecycleBoardStyles.category}>
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
      <div className={lifecycleBoardStyles.lanes}>
        {swimlanes.map(lane => {
          const laneStages = visibleStages
          if (!laneStages.length) return null

          return (
            <LifecycleLane
              key={lane.key}
              lane={lane}
              stages={laneStages}
              onSelectTask={onSelectTask}
              onAdvanceStage={onAdvanceStage}
              onMoveTaskToStage={onMoveTaskToStage}
            />
          )
        })}
      </div>
    </section>
  )
}
