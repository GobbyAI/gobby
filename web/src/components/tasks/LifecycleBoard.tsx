import { useEffect, useMemo, useState } from 'react'
import '../../styles/lifecycle-board.css'
import {
  taskAtStage,
  type LifecycleTask,
  type ReviewPolicy,
  type StageAdvanceAction,
  type StageStateView,
} from '../../lib/stageActions'
import { StageColumn, type StageRegistryEntry } from './StageColumn'

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

interface Swimlane {
  key: string
  label: string
  tasks: LifecycleTask[]
}

const HIDE_BLOCKED_KEY = 'lifecycle-board:hide-blocked'

function isTaskBlocked(task: LifecycleTask): boolean {
  return Boolean(task.is_blocked ?? task.state?.is_blocked)
}

function stageOrder(stage: StageRegistryEntry, index: number): number {
  return stage.sequence_order ?? index
}

function registryFromRows(tasks: LifecycleTask[]): StageRegistryEntry[] {
  const stages = new Map<string, StageRegistryEntry>()
  for (const task of tasks) {
    for (const row of task.stages ?? []) {
      if (stages.has(row.name)) continue
      stages.set(row.name, {
        name: row.name,
        display_name: row.display_name,
        category: row.category,
        review_policy: row.review_policy,
        sequence_order: row.position ?? stages.size,
      })
    }
  }
  return Array.from(stages.values()).sort((a, b) => stageOrder(a, 0) - stageOrder(b, 0))
}

function mergeRegistry(
  registry: ReadonlyArray<StageRegistryEntry> | undefined,
  tasks: LifecycleTask[],
): StageRegistryEntry[] {
  const fallback = registryFromRows(tasks)
  if (!registry?.length) return fallback

  const rowStages = new Map(fallback.map(stage => [stage.name, stage]))
  return registry.map(stage => ({
    ...stage,
    review_policy: stage.review_policy ?? rowStages.get(stage.name)?.review_policy ?? 'none',
  }))
}

function groupIntoSwimlanes(tasks: LifecycleTask[]): Swimlane[] {
  const groups = new Map<string, LifecycleTask[]>()
  for (const task of tasks) {
    const key = task.task_type || 'task'
    groups.set(key, [...(groups.get(key) ?? []), task])
  }
  return Array.from(groups.entries()).map(([key, laneTasks]) => ({
    key,
    label: key,
    tasks: laneTasks,
  }))
}

function stageNamesForTasks(tasks: LifecycleTask[]): Set<string> {
  const names = new Set<string>()
  for (const task of tasks) {
    for (const row of task.stages ?? []) names.add(row.name)
  }
  return names
}

function reviewPolicy(value: unknown): ReviewPolicy {
  if (value === 'required' || value === 'optional' || value === 'none') return value
  return 'none'
}

function normalizeRegistryEntry(row: StageStateView): StageRegistryEntry {
  return {
    name: row.name,
    display_name: row.display_name,
    category: row.category,
    review_policy: reviewPolicy(row.review_policy),
    sequence_order: row.position ?? null,
  }
}

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
    const merged = mergeRegistry(supplied, tasks)
    if (merged.length) return merged

    return tasks
      .flatMap(task => task.stages ?? [])
      .map(normalizeRegistryEntry)
      .sort((a, b) => stageOrder(a, 0) - stageOrder(b, 0))
  }, [registry, stagesRegistry, tasks])

  const visibleTasks = useMemo(
    () => (hideBlocked ? tasks.filter(task => !isTaskBlocked(task)) : tasks),
    [hideBlocked, tasks],
  )
  const categories = useMemo(
    () => Array.from(new Set(allStages.map(stage => stage.category))).sort(),
    [allStages],
  )
  const activeCategories = useMemo(
    () => selectedCategories ?? new Set(categories),
    [categories, selectedCategories],
  )
  const visibleStageNames = useMemo(() => stageNamesForTasks(visibleTasks), [visibleTasks])
  const visibleStages = useMemo(
    () => allStages
      .map((stage, index) => ({ stage, index }))
      .filter(({ stage }) => visibleStageNames.has(stage.name))
      .filter(({ stage }) => activeCategories.has(stage.category))
      .sort((a, b) => stageOrder(a.stage, a.index) - stageOrder(b.stage, b.index))
      .map(({ stage }) => stage),
    [activeCategories, allStages, visibleStageNames],
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
                    tasks={lane.tasks.filter(task => taskAtStage(task, stage.name))}
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
