import {
  taskAtStage,
  type LifecycleTask,
} from '../../lib/stageActions'
import type { StageRegistryEntry } from './StageColumn'

export interface Swimlane {
  key: string
  label: string
  tasks: LifecycleTask[]
}

export function isTaskBlocked(task: LifecycleTask): boolean {
  return Boolean(task.is_blocked ?? task.state?.is_blocked)
}

export function filterBlockedTasks(tasks: LifecycleTask[], hideBlocked: boolean): LifecycleTask[] {
  return hideBlocked ? tasks.filter(task => !isTaskBlocked(task)) : tasks
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

export function buildStageRegistry(
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

export function groupIntoSwimlanes(tasks: LifecycleTask[]): Swimlane[] {
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

export function stageNamesForTasks(tasks: LifecycleTask[]): Set<string> {
  const names = new Set<string>()
  for (const task of tasks) {
    for (const row of task.stages ?? []) names.add(row.name)
  }
  return names
}

export function stageCategories(stages: ReadonlyArray<StageRegistryEntry>): string[] {
  return Array.from(new Set(stages.map(stage => stage.category))).sort()
}

export function activeStageCategories(
  categories: string[],
  selectedCategories: Set<string> | null,
): Set<string> {
  return selectedCategories ?? new Set(categories)
}

export function visibleStagesForTasks(
  stages: ReadonlyArray<StageRegistryEntry>,
  tasks: LifecycleTask[],
  activeCategories: Set<string>,
): StageRegistryEntry[] {
  const visibleStageNames = stageNamesForTasks(tasks)
  return stages
    .map((stage, index) => ({ stage, index }))
    .filter(({ stage }) => visibleStageNames.has(stage.name))
    .filter(({ stage }) => activeCategories.has(stage.category))
    .sort((a, b) => stageOrder(a.stage, a.index) - stageOrder(b.stage, b.index))
    .map(({ stage }) => stage)
}

export function tasksForStage(tasks: LifecycleTask[], stageName: string): LifecycleTask[] {
  return tasks.filter(task => taskAtStage(task, stageName))
}
