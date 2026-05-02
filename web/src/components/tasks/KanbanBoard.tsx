import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { draggable, dropTargetForElements, monitorForElements } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge, BlockedIndicator, PRIORITY_STYLES } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'
import { RiskBadge } from './RiskBadges'
import { classifyTaskRisk } from './riskUtils'
import { ActivityPulse } from './ActivityPulse'
import { AssigneeBadge } from './AssigneeBadge'
import {
  getCanonicalTaskState,
  getTaskBucket,
  TASK_BUCKET_LABELS,
  type TaskBucket,
} from '../../lib/taskState'
import { cn } from '../../lib/utils'

interface KanbanColumnDef {
  key: TaskBucket
  label: string
  targetBucket: TaskBucket
  dotStatus: string
}

const COLUMNS: KanbanColumnDef[] = [
  { key: 'ready', label: TASK_BUCKET_LABELS.ready, targetBucket: 'ready', dotStatus: 'open' },
  { key: 'in_progress', label: TASK_BUCKET_LABELS.in_progress, targetBucket: 'in_progress', dotStatus: 'in_progress' },
  { key: 'review', label: TASK_BUCKET_LABELS.review, targetBucket: 'review', dotStatus: 'needs_review' },
  { key: 'blocked', label: TASK_BUCKET_LABELS.blocked, targetBucket: 'blocked', dotStatus: 'escalated' },
  { key: 'merge_ready', label: TASK_BUCKET_LABELS.merge_ready, targetBucket: 'merge_ready', dotStatus: 'review_approved' },
  { key: 'closed', label: TASK_BUCKET_LABELS.closed, targetBucket: 'closed', dotStatus: 'closed' },
]

const NEXT_BUCKET: Partial<Record<TaskBucket, TaskBucket>> = {
  ready: 'in_progress',
  in_progress: 'review',
  review: 'merge_ready',
  merge_ready: 'closed',
}

const ORDER_GAP = 1000
const ORDER_MIN = 0

const WRAPPER_CLS = 'flex flex-1 flex-col overflow-hidden'
const TOOLBAR_CLS = 'flex shrink-0 items-center gap-[0.35rem] border-b border-[var(--border)] px-2 py-[0.4rem]'
const TOOLBAR_LABEL_CLS = 'mr-[0.2rem] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const TOOLBAR_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-[0.2rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TOOLBAR_BTN_ACTIVE_CLS = 'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)]'

const SWIMLANE_CLS = 'flex min-h-0 flex-col only:flex-1 [&:only-child>div.kanban-board-inner]:flex-1'
const SWIMLANE_HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-[0.4rem] border-0 border-b border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-[0.45rem] text-left font-[inherit] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[color-mix(in_srgb,var(--color-info)_6%,transparent)] pointer-coarse:min-h-11'
const SWIMLANE_CHEVRON_CLS = 'w-[0.8rem] text-center text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const SWIMLANE_LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] font-semibold'
const SWIMLANE_COUNT_CLS =
  'min-w-[1.2rem] rounded-full bg-[var(--bg-secondary)] px-[0.35rem] py-[0.05rem] text-center text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'

const BOARD_CLS = 'kanban-board-inner flex flex-1 gap-3 overflow-x-auto overflow-y-hidden pb-2'

const COLUMN_CLS =
  'flex w-[220px] min-w-[220px] shrink-0 flex-col overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]'
const COLUMN_DRAG_OVER_CLS = 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-info)_6%,transparent)]'
const COLUMN_HEADER_CLS = 'flex items-center gap-[0.4rem] border-b border-[var(--border)] px-3 py-[0.6rem]'
const COLUMN_LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--text-primary)]'
const COLUMN_COUNT_CLS =
  'ml-auto min-w-[1.2rem] rounded-full bg-[var(--bg-tertiary)] px-[0.35rem] py-[0.05rem] text-center text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const COLUMN_BODY_CLS = 'flex flex-1 flex-col gap-[0.4rem] overflow-y-auto p-2'
const COLUMN_BODY_DRAG_OVER_CLS = 'min-h-[60px]'
const COLUMN_EMPTY_CLS =
  'px-2 py-4 text-center text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'

const CARD_CLS =
  'group flex w-full cursor-pointer flex-col gap-[0.3rem] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-[0.65rem] py-[0.6rem] text-left font-[inherit] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--accent)] hover:bg-[var(--bg-tertiary)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--accent)]'
const CARD_BLOCKED_CLS = 'cursor-not-allowed opacity-60'
const CARD_DRAGGING_CLS = 'opacity-40'
const CARD_DROP_TOP_CLS = 'shadow-[0_-2px_0_0_var(--accent)]'
const CARD_DROP_BOTTOM_CLS = 'shadow-[0_2px_0_0_var(--accent)]'

const CARD_HEADER_CLS = 'flex items-center justify-between gap-[0.3rem]'
const CARD_REF_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const CARD_TITLE_CLS =
  'overflow-hidden text-[length:calc(var(--font-size-base)*0.8)] leading-[1.35] text-[var(--text-primary)] [-webkit-box-orient:vertical] [-webkit-line-clamp:2] [display:-webkit-box]'
const CARD_FOOTER_CLS = 'mt-[0.1rem] flex items-center gap-[0.3rem]'

const CARD_ACTIONS_CLS = 'ml-auto hidden items-center gap-[0.2rem] group-hover:flex'
const CARD_ACTION_CLS =
  'inline-flex h-[1.2rem] w-[1.2rem] cursor-pointer items-center justify-center rounded-[0.2rem] bg-[var(--bg-tertiary)] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] transition-colors duration-100 hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)]'
const CARD_ACTION_CLOSE_CLS = 'hover:bg-[var(--color-success-foreground)] hover:text-[var(--accent-foreground)]'

function orderBetween(prev: number | null, next: number | null): number {
  const p = prev ?? ORDER_MIN
  const n = next ?? p + ORDER_GAP
  return p + (n - p) / 2
}

function ensureOrders(tasks: GobbyTask[]): GobbyTask[] {
  return tasks.map((t, i) => ({
    ...t,
    sequence_order: t.sequence_order ?? (i + 1) * ORDER_GAP,
  }))
}

function sortByOrder(tasks: GobbyTask[]): GobbyTask[] {
  return [...tasks].sort((a, b) => {
    const ao = a.sequence_order ?? Number.MAX_SAFE_INTEGER
    const bo = b.sequence_order ?? Number.MAX_SAFE_INTEGER
    if (ao !== bo) return ao - bo
    return new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  })
}

type SwimlaneModeType = 'none' | 'assignee' | 'priority' | 'parent'

interface Swimlane {
  key: string
  label: string
  tasks: GobbyTask[]
}

const PRIORITY_LABELS: Record<number, string> = {
  0: 'Critical',
  1: 'High',
  2: 'Medium',
  3: 'Low',
  4: 'Backlog',
}

function groupIntoSwimlanes(tasks: GobbyTask[], mode: SwimlaneModeType): Swimlane[] {
  if (mode === 'none') return [{ key: '_all', label: '', tasks }]

  const groups = new Map<string, GobbyTask[]>()

  for (const task of tasks) {
    let key: string
    if (mode === 'assignee') {
      key = getCanonicalTaskState(task).owner_session_id || '_unassigned'
    } else if (mode === 'priority') {
      key = String(task.priority)
    } else {
      key = task.parent_task_id || '_root'
    }
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(task)
  }

  const lanes: Swimlane[] = []

  if (mode === 'priority') {
    for (let p = 0; p <= 4; p++) {
      const key = String(p)
      if (groups.has(key)) {
        lanes.push({ key, label: PRIORITY_LABELS[p] || `P${p}`, tasks: groups.get(key)! })
      }
    }
  } else if (mode === 'assignee') {
    if (groups.has('_unassigned')) {
      lanes.push({ key: '_unassigned', label: 'Unassigned', tasks: groups.get('_unassigned')! })
      groups.delete('_unassigned')
    }
    for (const [key, tasks] of groups) {
      lanes.push({ key, label: key.slice(0, 12), tasks })
    }
  } else {
    if (groups.has('_root')) {
      lanes.push({ key: '_root', label: 'No Parent', tasks: groups.get('_root')! })
      groups.delete('_root')
    }
    for (const [key, tasks] of groups) {
      const parentRef = tasks[0]?.path_cache?.split('.')[0] || key.slice(0, 8)
      lanes.push({ key, label: `Parent ${parentRef}`, tasks })
    }
  }

  return lanes
}

interface KanbanCardProps {
  task: GobbyTask
  index: number
  columnKey: TaskBucket
  onSelect: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}

function KanbanCard({ task, index, columnKey, onSelect, onUpdateStatus }: KanbanCardProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [dropEdge, setDropEdge] = useState<'top' | 'bottom' | null>(null)
  const priorityColor = (PRIORITY_STYLES[task.priority] || PRIORITY_STYLES[2]).color
  const bucket = getTaskBucket(task)
  const ownerSessionId = getCanonicalTaskState(task).owner_session_id
  const isBlocked = bucket === 'blocked'
  const nextBucket = NEXT_BUCKET[bucket]
  const riskLevel = classifyTaskRisk(task.title, task.task_type)

  useEffect(() => {
    const el = ref.current
    if (!el || isBlocked) return
    return draggable({
      element: el,
      getInitialData: () => ({
        type: 'kanban-card',
        taskId: task.id,
        currentBucket: bucket,
        columnKey,
        index,
      }),
      onDragStart: () => setIsDragging(true),
      onDrop: () => setIsDragging(false),
    })
  }, [task.id, bucket, isBlocked, columnKey, index])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    return dropTargetForElements({
      element: el,
      getData: ({ input }) => {
        const rect = el.getBoundingClientRect()
        const midY = rect.top + rect.height / 2
        const edge = input.clientY < midY ? 'top' : 'bottom'
        return {
          type: 'kanban-card-target',
          taskId: task.id,
          columnKey,
          index,
          edge,
        }
      },
      canDrop: ({ source }) => source.data.type === 'kanban-card' && source.data.taskId !== task.id,
      onDragEnter: ({ self }) => setDropEdge(self.data.edge as 'top' | 'bottom'),
      onDrag: ({ self }) => setDropEdge(self.data.edge as 'top' | 'bottom'),
      onDragLeave: () => setDropEdge(null),
      onDrop: () => setDropEdge(null),
    })
  }, [task.id, columnKey, index])

  return (
    <div
      ref={ref}
      className={cn(
        CARD_CLS,
        isDragging && CARD_DRAGGING_CLS,
        isBlocked && CARD_BLOCKED_CLS,
        dropEdge === 'top' && CARD_DROP_TOP_CLS,
        dropEdge === 'bottom' && CARD_DROP_BOTTOM_CLS,
      )}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(task.id)}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(task.id) } }}
      style={{ borderLeftColor: priorityColor }}
    >
      <div className={CARD_HEADER_CLS}>
        <span className={CARD_REF_CLS}>{task.ref}</span>
        <ActivityPulse task={task} compact />
        {isBlocked ? <BlockedIndicator /> : <PriorityBadge priority={task.priority} />}
      </div>
      <div className={CARD_TITLE_CLS}>{task.title}</div>
      <div className={CARD_FOOTER_CLS}>
        <TypeBadge type={task.task_type} />
        <AssigneeBadge assignee={ownerSessionId} agentName={task.agent_name} />
        <RiskBadge level={riskLevel} compact />
        {onUpdateStatus && !isBlocked && (
          <div className={CARD_ACTIONS_CLS}>
            {nextBucket && (
              <button
                type="button"
                className={CARD_ACTION_CLS}
                title={`Move to ${TASK_BUCKET_LABELS[nextBucket]}`}
                onClick={e => { e.stopPropagation(); onUpdateStatus(task.id, nextBucket) }}
              >
                →
              </button>
            )}
            {bucket !== 'closed' && (
              <button
                type="button"
                className={cn(CARD_ACTION_CLS, CARD_ACTION_CLOSE_CLS)}
                title="Close task"
                onClick={e => { e.stopPropagation(); onUpdateStatus(task.id, 'closed') }}
              >
                ✓
              </button>
            )}
          </div>
        )}
      </div>
      <TaskStatusStrip task={task} />
    </div>
  )
}

function KanbanColumnComponent({
  col,
  tasks,
  onSelectTask,
  onUpdateStatus,
}: {
  col: KanbanColumnDef
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [isDraggedOver, setIsDraggedOver] = useState(false)

  const sorted = useMemo(() => sortByOrder(tasks), [tasks])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    return dropTargetForElements({
      element: el,
      getData: () => ({ type: 'kanban-column', columnKey: col.key, targetBucket: col.targetBucket }),
      canDrop: ({ source }) => source.data.type === 'kanban-card',
      onDragEnter: () => setIsDraggedOver(true),
      onDragLeave: () => setIsDraggedOver(false),
      onDrop: () => setIsDraggedOver(false),
    })
  }, [col.key, col.targetBucket])

  return (
    <div ref={ref} className={cn(COLUMN_CLS, isDraggedOver && COLUMN_DRAG_OVER_CLS)}>
      <div className={COLUMN_HEADER_CLS}>
        <StatusDot status={col.dotStatus} />
        <span className={COLUMN_LABEL_CLS}>{col.label}</span>
        <span className={COLUMN_COUNT_CLS} data-testid="kanban-column-count">{tasks.length}</span>
      </div>
      <div className={cn(COLUMN_BODY_CLS, isDraggedOver && COLUMN_BODY_DRAG_OVER_CLS)}>
        {sorted.length === 0 ? (
          <div className={COLUMN_EMPTY_CLS}>No tasks</div>
        ) : (
          sorted.map((task, i) => (
            <KanbanCard
              key={task.id}
              task={task}
              index={i}
              columnKey={col.key}
              onSelect={onSelectTask}
              onUpdateStatus={onUpdateStatus}
            />
          ))
        )}
      </div>
    </div>
  )
}

interface KanbanBoardProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
  onReorder?: (taskId: string, newOrder: number) => void
}

function groupByColumn(tasks: GobbyTask[]): Map<string, GobbyTask[]> {
  const grouped = new Map<string, GobbyTask[]>()
  for (const col of COLUMNS) {
    grouped.set(col.key, [])
  }
  for (const task of tasks) {
    const col = COLUMNS.find(c => c.key === getTaskBucket(task))
    if (col) {
      grouped.get(col.key)!.push(task)
    }
  }
  return grouped
}

export function KanbanBoard({ tasks, onSelectTask, onUpdateStatus, onReorder }: KanbanBoardProps) {
  const [swimlaneMode, setSwimlaneMode] = useState<SwimlaneModeType>('none')
  const [collapsedLanes, setCollapsedLanes] = useState<Set<string>>(new Set())

  const orderedTasks = useMemo(() => ensureOrders(tasks), [tasks])

  const columnTasksRef = useRef<Map<string, GobbyTask[]>>(new Map())
  useEffect(() => {
    const grouped = groupByColumn(orderedTasks)
    for (const [key, list] of grouped) {
      grouped.set(key, sortByOrder(list))
    }
    columnTasksRef.current = grouped
  }, [orderedTasks])

  const handleReorder = useCallback(
    (taskId: string, targetColumnKey: string, insertIndex: number) => {
      if (!onReorder) return
      const colTasks = columnTasksRef.current.get(targetColumnKey) || []
      const filtered = colTasks.filter(t => t.id !== taskId)

      let newOrder: number
      if (filtered.length === 0) {
        newOrder = ORDER_GAP
      } else if (insertIndex <= 0) {
        newOrder = orderBetween(null, filtered[0].sequence_order)
      } else if (insertIndex >= filtered.length) {
        newOrder = orderBetween(filtered[filtered.length - 1].sequence_order, null)
      } else {
        newOrder = orderBetween(
          filtered[insertIndex - 1].sequence_order,
          filtered[insertIndex].sequence_order
        )
      }

      onReorder(taskId, newOrder)
    },
    [onReorder]
  )

  useEffect(() => {
    return monitorForElements({
      canMonitor: ({ source }) => source.data.type === 'kanban-card',
      onDrop: ({ source, location }) => {
        const dropTargets = location.current.dropTargets
        if (dropTargets.length === 0) return

        const taskId = source.data.taskId as string
        const currentBucket = source.data.currentBucket as string

        const innermost = dropTargets[0]
        if (innermost.data.type === 'kanban-card-target') {
          const targetColumnKey = innermost.data.columnKey as string
          const targetIndex = innermost.data.index as number
          const edge = innermost.data.edge as string

          const columnTarget = dropTargets.find(t => t.data.type === 'kanban-column')
          const targetBucket = columnTarget?.data.targetBucket as string | undefined

          if (targetBucket && currentBucket !== targetBucket && onUpdateStatus) {
            onUpdateStatus(taskId, targetBucket)
          }

          const insertIndex = edge === 'top' ? targetIndex : targetIndex + 1
          handleReorder(taskId, targetColumnKey, insertIndex)
          return
        }

        if (innermost.data.type === 'kanban-column') {
          const targetBucket = innermost.data.targetBucket as string
          if (currentBucket !== targetBucket && onUpdateStatus) {
            onUpdateStatus(taskId, targetBucket)
          }
          if (onReorder) {
            const targetColumnKey = innermost.data.columnKey as string
            const colTasks = columnTasksRef.current.get(targetColumnKey) || []
            const filtered = colTasks.filter(t => t.id !== taskId)
            const lastOrder = filtered.length > 0
              ? filtered[filtered.length - 1].sequence_order ?? ORDER_GAP
              : 0
            onReorder(taskId, lastOrder + ORDER_GAP)
          }
        }
      },
    })
  }, [onUpdateStatus, onReorder, handleReorder])

  const swimlanes = useMemo(() => groupIntoSwimlanes(orderedTasks, swimlaneMode), [orderedTasks, swimlaneMode])

  const toggleLane = (key: string) => {
    setCollapsedLanes(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className={WRAPPER_CLS}>
      <div className={TOOLBAR_CLS}>
        <span className={TOOLBAR_LABEL_CLS}>Group by:</span>
        {(['none', 'assignee', 'priority', 'parent'] as const).map(mode => (
          <button
            key={mode}
            className={cn(TOOLBAR_BTN_CLS, swimlaneMode === mode && TOOLBAR_BTN_ACTIVE_CLS)}
            onClick={() => { setSwimlaneMode(mode); setCollapsedLanes(new Set()) }}
          >
            {mode === 'none' ? 'None' : mode.charAt(0).toUpperCase() + mode.slice(1)}
          </button>
        ))}
      </div>

      {swimlanes.map(lane => {
        const isCollapsed = collapsedLanes.has(lane.key)
        const grouped = groupByColumn(lane.tasks)

        return (
          <div key={lane.key} className={SWIMLANE_CLS}>
            {swimlaneMode !== 'none' && (
              <button className={SWIMLANE_HEADER_CLS} onClick={() => toggleLane(lane.key)}>
                <span className={SWIMLANE_CHEVRON_CLS}>{isCollapsed ? '▸' : '▾'}</span>
                <span className={SWIMLANE_LABEL_CLS}>{lane.label}</span>
                <span className={SWIMLANE_COUNT_CLS}>{lane.tasks.length}</span>
              </button>
            )}
            {!isCollapsed && (
              <div className={BOARD_CLS}>
                {COLUMNS.map(col => (
                  <KanbanColumnComponent
                    key={col.key}
                    col={col}
                    tasks={grouped.get(col.key) || []}
                    onSelectTask={onSelectTask}
                    onUpdateStatus={onUpdateStatus}
                  />
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
