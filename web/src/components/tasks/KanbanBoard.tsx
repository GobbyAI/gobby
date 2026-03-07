import { useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { draggable, dropTargetForElements, monitorForElements } from '@atlaskit/pragmatic-drag-and-drop/element/adapter'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge, BlockedIndicator, PRIORITY_STYLES } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'
import { classifyTaskRisk, RiskBadge } from './RiskBadges'
import { ActivityPulse } from './ActivityPulse'
import { AssigneeBadge } from './AssigneePicker'

// =============================================================================
// Column definitions: map 8 statuses → 6 columns
// =============================================================================

interface KanbanColumnDef {
  key: string
  label: string
  statuses: string[]
  targetStatus: string
}

const COLUMNS: KanbanColumnDef[] = [
  { key: 'backlog',     label: 'Backlog',     statuses: ['open'],                targetStatus: 'open' },
  { key: 'in_progress', label: 'In Progress', statuses: ['in_progress'],         targetStatus: 'in_progress' },
  { key: 'review',      label: 'Review',      statuses: ['needs_review'],        targetStatus: 'needs_review' },
  { key: 'blocked',     label: 'Blocked',     statuses: ['escalated'],           targetStatus: 'escalated' },
  { key: 'approved',    label: 'Ready',       statuses: ['review_approved'],     targetStatus: 'review_approved' },
  { key: 'closed',      label: 'Closed',      statuses: ['closed'],              targetStatus: 'closed' },
]

// Status progression: current → next
const NEXT_STATUS: Record<string, string> = {
  open: 'in_progress',
  in_progress: 'needs_review',
  needs_review: 'review_approved',
  review_approved: 'closed',
}

const BLOCKED_STATUSES = new Set(['escalated'])

// =============================================================================
// Fractional indexing helpers
// =============================================================================

const ORDER_GAP = 1000
const ORDER_MIN = 0

/** Compute a sequence_order between two neighbors using float midpoint for ~53 levels of precision. */
function orderBetween(prev: number | null, next: number | null): number {
  const p = prev ?? ORDER_MIN
  const n = next ?? p + ORDER_GAP
  return p + (n - p) / 2
}

/** Assign initial orders to tasks that have no sequence_order, preserving existing ones. */
function ensureOrders(tasks: GobbyTask[]): GobbyTask[] {
  return tasks.map((t, i) => ({
    ...t,
    sequence_order: t.sequence_order ?? (i + 1) * ORDER_GAP,
  }))
}

/** Sort by sequence_order (nulls last), then created_at. */
function sortByOrder(tasks: GobbyTask[]): GobbyTask[] {
  return [...tasks].sort((a, b) => {
    const ao = a.sequence_order ?? Number.MAX_SAFE_INTEGER
    const bo = b.sequence_order ?? Number.MAX_SAFE_INTEGER
    if (ao !== bo) return ao - bo
    return new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  })
}

// =============================================================================
// Swimlane grouping
// =============================================================================

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
      key = task.assignee || '_unassigned'
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

// =============================================================================
// KanbanCard (draggable + drop target for reorder)
// =============================================================================

interface KanbanCardProps {
  task: GobbyTask
  index: number
  columnKey: string
  onSelect: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}

function KanbanCard({ task, index, columnKey, onSelect, onUpdateStatus }: KanbanCardProps) {
  const ref = useRef<HTMLDivElement | null>(null)
  const [isDragging, setIsDragging] = useState(false)
  const [dropEdge, setDropEdge] = useState<'top' | 'bottom' | null>(null)
  const priorityColor = (PRIORITY_STYLES[task.priority] || PRIORITY_STYLES[2]).color
  const isBlocked = BLOCKED_STATUSES.has(task.status)
  const nextStatus = NEXT_STATUS[task.status]
  const riskLevel = classifyTaskRisk(task.title, task.task_type)

  // Draggable
  useEffect(() => {
    const el = ref.current
    if (!el || isBlocked) return
    return draggable({
      element: el,
      getInitialData: () => ({
        type: 'kanban-card',
        taskId: task.id,
        currentStatus: task.status,
        columnKey,
        index,
      }),
      onDragStart: () => setIsDragging(true),
      onDrop: () => setIsDragging(false),
    })
  }, [task.id, task.status, isBlocked, columnKey, index])

  // Drop target (for within-column reorder)
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

  const classes = [
    'kanban-card',
    isDragging ? 'kanban-card--dragging' : '',
    isBlocked ? 'kanban-card--blocked' : '',
    dropEdge === 'top' ? 'kanban-card--drop-top' : '',
    dropEdge === 'bottom' ? 'kanban-card--drop-bottom' : '',
  ].filter(Boolean).join(' ')

  return (
    <div
      ref={ref}
      className={classes}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(task.id)}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(task.id) } }}
      style={{ borderLeftColor: priorityColor }}
    >
      <div className="kanban-card-header">
        <span className="kanban-card-ref">{task.ref}</span>
        <ActivityPulse task={task} compact />
        {isBlocked ? <BlockedIndicator /> : <PriorityBadge priority={task.priority} />}
      </div>
      <div className="kanban-card-title">{task.title}</div>
      <div className="kanban-card-footer">
        <TypeBadge type={task.task_type} />
        <AssigneeBadge assignee={task.assignee} agentName={task.agent_name} />
        <RiskBadge level={riskLevel} compact />
        {onUpdateStatus && !isBlocked && (
          <div className="kanban-card-actions">
            {nextStatus && (
              <button
                type="button"
                className="kanban-card-action"
                title={`Move to ${nextStatus.replace(/_/g, ' ')}`}
                onClick={e => { e.stopPropagation(); onUpdateStatus(task.id, nextStatus) }}
              >
                →
              </button>
            )}
            {task.status !== 'closed' && (
              <button
                type="button"
                className="kanban-card-action kanban-card-action--close"
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

// =============================================================================
// KanbanColumn (drop target)
// =============================================================================

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

  // Sort tasks by sequence_order within the column
  const sorted = useMemo(() => sortByOrder(tasks), [tasks])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    return dropTargetForElements({
      element: el,
      getData: () => ({ type: 'kanban-column', columnKey: col.key, targetStatus: col.targetStatus }),
      canDrop: ({ source }) => source.data.type === 'kanban-card',
      onDragEnter: () => setIsDraggedOver(true),
      onDragLeave: () => setIsDraggedOver(false),
      onDrop: () => setIsDraggedOver(false),
    })
  }, [col.key, col.targetStatus])

  return (
    <div ref={ref} className={`kanban-column ${isDraggedOver ? 'kanban-column--drag-over' : ''}`}>
      <div className="kanban-column-header">
        <StatusDot status={col.statuses[0]} />
        <span className="kanban-column-label">{col.label}</span>
        <span className="kanban-column-count">{tasks.length}</span>
      </div>
      <div className="kanban-column-body">
        {sorted.length === 0 ? (
          <div className="kanban-column-empty">No tasks</div>
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

// =============================================================================
// KanbanBoard
// =============================================================================

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
    const col = COLUMNS.find(c => c.statuses.includes(task.status))
    if (col) {
      grouped.get(col.key)!.push(task)
    }
  }
  return grouped
}

export function KanbanBoard({ tasks, onSelectTask, onUpdateStatus, onReorder }: KanbanBoardProps) {
  const [swimlaneMode, setSwimlaneMode] = useState<SwimlaneModeType>('none')
  const [collapsedLanes, setCollapsedLanes] = useState<Set<string>>(new Set())

  // Ensure all tasks have a sequence_order for sorting
  const orderedTasks = useMemo(() => ensureOrders(tasks), [tasks])

  // Build a lookup from column key → sorted task list (for order calculations)
  const columnTasksRef = useRef<Map<string, GobbyTask[]>>(new Map())
  useEffect(() => {
    const grouped = groupByColumn(orderedTasks)
    for (const [key, list] of grouped) {
      grouped.set(key, sortByOrder(list))
    }
    columnTasksRef.current = grouped
  }, [orderedTasks])

  // Handle reorder within column
  const handleReorder = useCallback(
    (taskId: string, targetColumnKey: string, insertIndex: number) => {
      if (!onReorder) return
      const colTasks = columnTasksRef.current.get(targetColumnKey) || []
      // Remove the dragged task from the list to get clean neighbors
      const filtered = colTasks.filter(t => t.id !== taskId)

      let newOrder: number
      if (filtered.length === 0) {
        newOrder = ORDER_GAP
      } else if (insertIndex <= 0) {
        // Before first
        newOrder = orderBetween(null, filtered[0].sequence_order)
      } else if (insertIndex >= filtered.length) {
        // After last
        newOrder = orderBetween(filtered[filtered.length - 1].sequence_order, null)
      } else {
        // Between two
        newOrder = orderBetween(
          filtered[insertIndex - 1].sequence_order,
          filtered[insertIndex].sequence_order
        )
      }

      onReorder(taskId, newOrder)
    },
    [onReorder]
  )

  // Monitor for drops globally
  useEffect(() => {
    return monitorForElements({
      canMonitor: ({ source }) => source.data.type === 'kanban-card',
      onDrop: ({ source, location }) => {
        const dropTargets = location.current.dropTargets
        if (dropTargets.length === 0) return

        const taskId = source.data.taskId as string
        const currentStatus = source.data.currentStatus as string

        // Check if innermost target is a card (reorder within column)
        const innermost = dropTargets[0]
        if (innermost.data.type === 'kanban-card-target') {
          const targetColumnKey = innermost.data.columnKey as string
          const targetIndex = innermost.data.index as number
          const edge = innermost.data.edge as string

          // Find the column drop target for status change
          const columnTarget = dropTargets.find(t => t.data.type === 'kanban-column')
          const targetStatus = columnTarget?.data.targetStatus as string | undefined

          // If cross-column, update status first
          if (targetStatus && currentStatus !== targetStatus && onUpdateStatus) {
            onUpdateStatus(taskId, targetStatus)
          }

          // Compute insert index based on edge
          const insertIndex = edge === 'top' ? targetIndex : targetIndex + 1
          handleReorder(taskId, targetColumnKey, insertIndex)
          return
        }

        // Fallback: column-level drop (status change only, append to end)
        if (innermost.data.type === 'kanban-column') {
          const targetStatus = innermost.data.targetStatus as string
          if (currentStatus !== targetStatus && onUpdateStatus) {
            onUpdateStatus(taskId, targetStatus)
          }
          // Place at end of target column
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
    <div className="kanban-wrapper">
      {/* Swimlane toolbar */}
      <div className="kanban-toolbar">
        <span className="kanban-toolbar-label">Group by:</span>
        {(['none', 'assignee', 'priority', 'parent'] as const).map(mode => (
          <button
            key={mode}
            className={`kanban-toolbar-btn ${swimlaneMode === mode ? 'active' : ''}`}
            onClick={() => { setSwimlaneMode(mode); setCollapsedLanes(new Set()) }}
          >
            {mode === 'none' ? 'None' : mode.charAt(0).toUpperCase() + mode.slice(1)}
          </button>
        ))}
      </div>

      {/* Board with optional swimlanes */}
      {swimlanes.map(lane => {
        const isCollapsed = collapsedLanes.has(lane.key)
        const grouped = groupByColumn(lane.tasks)

        return (
          <div key={lane.key} className="kanban-swimlane">
            {swimlaneMode !== 'none' && (
              <button className="kanban-swimlane-header" onClick={() => toggleLane(lane.key)}>
                <span className="kanban-swimlane-chevron">{isCollapsed ? '\u25B8' : '\u25BE'}</span>
                <span className="kanban-swimlane-label">{lane.label}</span>
                <span className="kanban-swimlane-count">{lane.tasks.length}</span>
              </button>
            )}
            {!isCollapsed && (
              <div className="kanban-board">
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
