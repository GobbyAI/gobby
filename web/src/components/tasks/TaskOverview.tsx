import type { TaskStats } from '../../hooks/useTasks'
import { TASK_BUCKET_LABELS, type TaskBucket } from '../../lib/taskState'

// =============================================================================
// TaskOverview
// =============================================================================

interface TaskOverviewProps {
  stats: TaskStats
  activeFilter: string | null
  onFilterStatus: (status: string | null) => void
}

export function TaskOverview({
  stats,
  activeFilter,
  onFilterStatus,
}: TaskOverviewProps) {
  const cards = [
    {
      key: 'ready',
      label: TASK_BUCKET_LABELS.ready,
      count: stats.ready || 0,
      filterStatus: 'ready',
      className: 'task-overview-card--now',
    },
    {
      key: 'in_progress',
      label: TASK_BUCKET_LABELS.in_progress,
      count: stats.in_progress || 0,
      filterStatus: 'in_progress',
      className: 'task-overview-card--progress',
    },
    {
      key: 'review',
      label: TASK_BUCKET_LABELS.review,
      count: stats.review || 0,
      filterStatus: 'review',
      className: 'task-overview-card--review',
    },
    {
      key: 'merge_ready',
      label: TASK_BUCKET_LABELS.merge_ready,
      count: stats.merge_ready || 0,
      filterStatus: 'merge_ready',
      className: 'task-overview-card--approved',
    },
    {
      key: 'blocked',
      label: TASK_BUCKET_LABELS.blocked,
      count: stats.blocked || 0,
      filterStatus: 'blocked',
      className: 'task-overview-card--escalated',
    },
    {
      key: 'closed',
      label: TASK_BUCKET_LABELS.closed,
      count: stats.closed || 0,
      filterStatus: 'closed',
      className: 'task-overview-card--recent',
    },
  ] satisfies Array<{
    key: TaskBucket
    label: string
    count: number
    filterStatus: TaskBucket
    className: string
  }>

  return (
    <div className="task-overview">
      {cards.map((card) => (
        <button
          key={card.key}
          className={`task-overview-card ${card.className} ${activeFilter === card.filterStatus ? 'task-overview-card--active' : ''}`}
          onClick={() =>
            onFilterStatus(
              activeFilter === card.filterStatus ? null : card.filterStatus,
            )
          }
        >
          <span className="task-overview-count">{card.count}</span>
          <span className="task-overview-label">{card.label}</span>
        </button>
      ))}
    </div>
  )
}
