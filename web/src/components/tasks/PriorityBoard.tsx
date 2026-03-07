import { useMemo } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'

// =============================================================================
// Column definitions
// =============================================================================

interface PriorityColumnDef {
  key: 'now' | 'next' | 'later'
  label: string
  color: string
  description: string
}

const COLUMNS: PriorityColumnDef[] = [
  { key: 'now',   label: 'Now',   color: '#ef4444', description: 'Active + Critical/High' },
  { key: 'next',  label: 'Next',  color: '#f59e0b', description: 'Medium priority, ready' },
  { key: 'later', label: 'Later', color: '#737373', description: 'Low + Backlog' },
]

const DONE_STATUSES = new Set(['closed', 'review_approved'])

function classifyTask(task: GobbyTask): 'now' | 'next' | 'later' | null {
  // Skip completed tasks
  if (DONE_STATUSES.has(task.status)) return null

  // In-progress or blocked tasks with high urgency → Now
  if (task.status === 'in_progress') return 'now'
  if (task.priority <= 1) return 'now'

  // Medium priority, open/review → Next
  if (task.priority === 2) return 'next'

  // Low/Backlog → Later
  return 'later'
}

function groupByPriority(tasks: GobbyTask[]): Map<string, GobbyTask[]> {
  const grouped = new Map<string, GobbyTask[]>()
  for (const col of COLUMNS) grouped.set(col.key, [])

  for (const task of tasks) {
    const col = classifyTask(task)
    if (col) grouped.get(col)!.push(task)
  }

  // Sort within columns: by priority, then by updated_at desc
  for (const [, list] of grouped) {
    list.sort((a, b) => {
      if (a.priority !== b.priority) return a.priority - b.priority
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    })
  }

  return grouped
}

// =============================================================================
// PriorityCard
// =============================================================================

function PriorityCard({
  task,
  onSelect,
  onUpdateStatus,
}: {
  task: GobbyTask
  onSelect: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}) {
  return (
    <button
      className="priority-card"
      onClick={() => onSelect(task.id)}
    >
      <div className="priority-card-header">
        <StatusDot status={task.status} />
        <span className="priority-card-ref">{task.ref}</span>
        <PriorityBadge priority={task.priority} />
      </div>
      <div className="priority-card-title">{task.title}</div>
      <div className="priority-card-footer">
        <TypeBadge type={task.task_type} />
        <span className="priority-card-status">{task.status.replace(/_/g, ' ')}</span>
        {onUpdateStatus && task.status === 'open' && (
          <button
            type="button"
            className="priority-card-action"
            title="Start work"
            onClick={e => { e.stopPropagation(); onUpdateStatus(task.id, 'in_progress') }}
          >
            ▶
          </button>
        )}
      </div>
      <TaskStatusStrip task={task} compact />
    </button>
  )
}

// =============================================================================
// PriorityColumn
// =============================================================================

function PriorityColumn({
  col,
  tasks,
  onSelectTask,
  onUpdateStatus,
}: {
  col: PriorityColumnDef
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}) {
  return (
    <div className="priority-column">
      <div className="priority-column-header">
        <span className="priority-column-dot" style={{ background: col.color }} />
        <span className="priority-column-label">{col.label}</span>
        <span className="priority-column-count">{tasks.length}</span>
      </div>
      <div className="priority-column-desc">{col.description}</div>
      <div className="priority-column-body">
        {tasks.length === 0 ? (
          <div className="priority-column-empty">No tasks</div>
        ) : (
          tasks.map(task => (
            <PriorityCard
              key={task.id}
              task={task}
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
// PriorityBoard
// =============================================================================

interface PriorityBoardProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
  onUpdateStatus?: (taskId: string, newStatus: string) => void
}

export function PriorityBoard({ tasks, onSelectTask, onUpdateStatus }: PriorityBoardProps) {
  const grouped = useMemo(() => groupByPriority(tasks), [tasks])
  const doneCount = useMemo(
    () => tasks.filter(t => DONE_STATUSES.has(t.status)).length,
    [tasks]
  )

  return (
    <div className="priority-board-wrapper">
      <div className="priority-board">
        {COLUMNS.map(col => (
          <PriorityColumn
            key={col.key}
            col={col}
            tasks={grouped.get(col.key) || []}
            onSelectTask={onSelectTask}
            onUpdateStatus={onUpdateStatus}
          />
        ))}
      </div>
      {doneCount > 0 && (
        <div className="priority-done-summary">
          {doneCount} completed task{doneCount !== 1 ? 's' : ''} hidden
        </div>
      )}
    </div>
  )
}
