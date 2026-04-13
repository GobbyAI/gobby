import { useEffect, useState } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { relativeTime } from '../../utils/formatTime'
import { getCanonicalTaskState, getTaskBucketLabel } from '../../lib/taskState'

// =============================================================================
// Status label mapping
// =============================================================================

// =============================================================================
// TaskStatusStrip
// =============================================================================

interface TaskStatusStripProps {
  task: GobbyTask
  compact?: boolean
}

export function TaskStatusStrip({ task, compact }: TaskStatusStripProps) {
  const state = getCanonicalTaskState(task)
  const bucketLabel = getTaskBucketLabel(task)
  const isActive = !state.is_closed && (state.is_claimed || state.lifecycle_stage === 'in_progress')
  const ownerLabel = task.agent_name || (state.owner_session_id ? `#${state.owner_session_id.slice(0, 6)}` : null)

  // Live-updating relative timestamp
  const [timeLabel, setTimeLabel] = useState(() => relativeTime(task.updated_at))
  useEffect(() => {
    setTimeLabel(relativeTime(task.updated_at))
    if (!isActive) return
    const interval = window.setInterval(() => setTimeLabel(relativeTime(task.updated_at)), 30000)
    return () => window.clearInterval(interval)
  }, [task.updated_at, isActive])

  // Only show strip if task has active ownership or non-ready workflow state
  if (!ownerLabel && !isActive && bucketLabel === 'Ready') return null

  return (
    <div className={`task-status-strip ${isActive ? 'task-status-strip--active' : ''} ${compact ? 'task-status-strip--compact' : ''}`}>
      {isActive && <span className="task-status-strip-pulse" />}
      {ownerLabel && <span className="task-status-strip-agent">{ownerLabel}</span>}
      <span className="task-status-strip-step">{bucketLabel}</span>
      <span className="task-status-strip-time">{timeLabel}</span>
    </div>
  )
}
