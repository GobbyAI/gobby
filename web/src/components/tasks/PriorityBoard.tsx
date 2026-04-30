import { useMemo } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { TaskStatusStrip } from './TaskStatusStrip'
import { getTaskBucket, getTaskStateSummary } from '../../lib/taskState'

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
  { key: 'now',   label: 'Now',   color: 'var(--color-error)', description: 'Active + Critical/High' },
  { key: 'next',  label: 'Next',  color: 'var(--color-warning-foreground)', description: 'Medium priority, ready' },
  { key: 'later', label: 'Later', color: 'var(--text-muted)', description: 'Low + Backlog' },
]

function classifyTask(task: GobbyTask): 'now' | 'next' | 'later' | null {
  const bucket = getTaskBucket(task)

  if (bucket === 'closed') return null

  // In-progress or blocked tasks with high urgency → Now
  if (bucket === 'in_progress' || bucket === 'blocked') return 'now'
  if (task.priority <= 1) return 'now'

  // Review and merge-ready work stays near the front of the queue.
  if (bucket === 'review' || bucket === 'merge_ready') return 'next'
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
        <StatusDot task={task} />
        <span className="priority-card-ref">{task.ref}</span>
        <PriorityBadge priority={task.priority} />
      </div>
      <div className="priority-card-title">{task.title}</div>
      <div className="priority-card-footer">
        <TypeBadge type={task.task_type} />
        <span className="priority-card-status">{getTaskStateSummary(task)}</span>
        {onUpdateStatus && getTaskBucket(task) === 'ready' && (
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
    () => tasks.filter(t => getTaskBucket(t) === 'closed').length,
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
