import type { GobbyTask } from '../../hooks/useTasks'
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  TASK_STATE_COLORS,
  TASK_STATE_LABELS,
  type TaskDisplayState,
} from '../../lib/taskState'
import type { StageState5 } from '../../lib/stageActions'

export interface TreeNode {
  id: string
  task: GobbyTask
  children: TreeNode[]
}

export interface VisibleTaskRow {
  node: TreeNode
  depth: number
  isInternal: boolean
  isOpen: boolean
}

export type TaskFilterKey = TaskDisplayState | 'escalated' | 'review_rejected'

export const STAGE_STATE_FILTERS: TaskFilterKey[] = [
  'ready',
  'in_progress',
  'needs_review',
  'review_approved',
  'review_rejected',
]
export const STATUS_FILTERS: TaskFilterKey[] = ['blocked', 'escalated', 'closed']
export const DEFAULT_FILTERS = new Set<TaskFilterKey>([
  ...STAGE_STATE_FILTERS,
  'blocked',
  'escalated',
])
export const RECENT_CLOSED_TASK_LIMIT = 20
const MAX_TASK_TREE_DEPTH = 100

export const PRIORITY_TEXT_COLORS: Record<number, string> = {
  0: 'var(--text-primary)',
  1: 'var(--text-primary)',
  2: 'var(--text-primary)',
  3: 'var(--text-secondary)',
  4: 'var(--text-muted)',
}

export const PRIORITY_TEXT_WEIGHTS: Record<number, string> = {
  0: 'var(--font-weight-semibold)',
  1: 'var(--font-weight-semibold)',
  2: 'var(--font-weight-medium)',
  3: 'var(--font-weight-medium)',
  4: 'var(--font-weight-medium)',
}

export function compareTasksForDisplay(a: GobbyTask, b: GobbyTask): number {
  const priorityDiff = (a.priority ?? 4) - (b.priority ?? 4)
  if (priorityDiff !== 0) return priorityDiff

  const seqA = a.seq_num ?? Number.MAX_SAFE_INTEGER
  const seqB = b.seq_num ?? Number.MAX_SAFE_INTEGER
  if (seqA !== seqB) return seqA - seqB

  const createdAtDiff = (a.created_at ?? '').localeCompare(b.created_at ?? '')
  if (createdAtDiff !== 0) return createdAtDiff

  return (a.updated_at ?? '').localeCompare(b.updated_at ?? '')
}

export function buildTree(tasks: GobbyTask[]): TreeNode[] {
  const nodeMap = new Map<string, TreeNode>()
  const roots: TreeNode[] = []

  for (const task of tasks) {
    nodeMap.set(task.id, { id: task.id, task, children: [] })
  }

  for (const task of tasks) {
    const node = nodeMap.get(task.id)!
    if (
      task.parent_task_id &&
      nodeMap.has(task.parent_task_id) &&
      !parentWouldCreateCycle(task, nodeMap)
    ) {
      nodeMap.get(task.parent_task_id)!.children.push(node)
    } else {
      roots.push(node)
    }
  }

  for (const node of nodeMap.values()) {
    node.children.sort((left, right) => compareTasksForDisplay(left.task, right.task))
  }
  roots.sort((left, right) => compareTasksForDisplay(left.task, right.task))

  return roots
}

function parentWouldCreateCycle(
  task: GobbyTask,
  nodeMap: Map<string, TreeNode>,
): boolean {
  let parentId = task.parent_task_id
  let depth = 0
  while (parentId && depth < MAX_TASK_TREE_DEPTH) {
    if (parentId === task.id) return true
    parentId = nodeMap.get(parentId)?.task.parent_task_id ?? null
    depth += 1
  }
  return depth >= MAX_TASK_TREE_DEPTH
}

function taskMatchesSearch(task: GobbyTask, term: string): boolean {
  const lower = term.toLowerCase()
  return task.title.toLowerCase().includes(lower) || task.ref.toLowerCase().includes(lower)
}

export function filterTreeBySearch(nodes: TreeNode[], term: string): TreeNode[] {
  const trimmed = term.trim()
  if (!trimmed) return nodes

  const visit = (node: TreeNode): TreeNode | null => {
    const children = node.children
      .map(visit)
      .filter((child): child is TreeNode => child !== null)

    if (!taskMatchesSearch(node.task, trimmed) && children.length === 0) return null
    return { ...node, children }
  }

  return nodes.map(visit).filter((node): node is TreeNode => node !== null)
}

export function collectExpandableNodeIds(
  nodes: TreeNode[],
  ids: Set<string> = new Set(),
  ancestors: Set<string> = new Set(),
): Set<string> {
  for (const node of nodes) {
    if (ancestors.has(node.id)) continue
    if (node.children.length > 0) {
      ids.add(node.id)
      collectExpandableNodeIds(node.children, ids, new Set(ancestors).add(node.id))
    }
  }
  return ids
}

export function collectVisibleTaskRows(
  nodes: TreeNode[],
  collapsedIds: Set<string>,
  depth = 0,
  forceOpen = false,
  ancestors: Set<string> = new Set(),
): VisibleTaskRow[] {
  if (depth >= MAX_TASK_TREE_DEPTH) return []
  return nodes.flatMap(node => {
    if (ancestors.has(node.id)) return []
    const isInternal = node.children.length > 0
    const isOpen = forceOpen || !collapsedIds.has(node.id)
    const row: VisibleTaskRow = { node, depth, isInternal, isOpen }

    const nextAncestors = new Set(ancestors).add(node.id)
    if (!isInternal || !isOpen) return [row]
    return [
      row,
      ...collectVisibleTaskRows(
        node.children,
        collapsedIds,
        depth + 1,
        forceOpen,
        nextAncestors,
      ),
    ]
  })
}

export function getTaskFilterLabel(filter: TaskFilterKey): string {
  if (filter === 'escalated') return 'Escalated'
  if (filter === 'review_rejected') return 'Review Rejected'
  return TASK_STATE_LABELS[filter]
}

export function getTaskFilterColor(filter: TaskFilterKey): string {
  if (filter === 'escalated') return 'var(--color-error)'
  if (filter === 'review_rejected') return 'var(--color-warning-foreground)'
  return TASK_STATE_COLORS[filter] ?? '#737373'
}

export function getStageStateColor(state: StageState5): string {
  return state === 'done' ? TASK_STATE_COLORS.closed : TASK_STATE_COLORS[state]
}

function hasRejectedCurrentReview(task: GobbyTask): boolean {
  const currentStage = getCanonicalTaskState(task).current_stage
  return currentStage?.state === 'ready' && (currentStage.review_round_count ?? 0) > 0
}

export function matchesTaskFilter(task: GobbyTask, filters: Set<TaskFilterKey>): boolean {
  const state = getCanonicalTaskState(task)
  if (state.is_closed) return filters.has('closed')
  if (state.is_escalated) return filters.has('escalated')
  if (hasRejectedCurrentReview(task)) return filters.has('review_rejected')
  return filters.has(getTaskDisplayState(task))
}
