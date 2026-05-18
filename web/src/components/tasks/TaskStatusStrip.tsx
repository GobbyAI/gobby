import { useEffect, useState } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { relativeTime } from '../../utils/formatTime'
import { getCanonicalTaskState, getTaskDisplayState, getTaskStateLabel } from '../../lib/taskState'

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
  const displayState = getTaskDisplayState(task)
  const stateLabel = getTaskStateLabel(task)
  const isActive = !state.is_closed && (state.is_claimed || displayState === 'in_progress')
  // Friendly owner ref (#<seq_num> or UUID prefix fallback).
  const ownerLabel =
    task.agent_name ||
    state.owner_session_ref?.ref ||
    (state.owner_session_id ? `#${state.owner_session_id.slice(0, 6)}` : null)

  // Live-updating relative timestamp
  const [timeLabel, setTimeLabel] = useState(() => relativeTime(task.updated_at))
  useEffect(() => {
    setTimeLabel(relativeTime(task.updated_at))
    if (!isActive) return
    const interval = window.setInterval(() => setTimeLabel(relativeTime(task.updated_at)), 30000)
    return () => window.clearInterval(interval)
  }, [task.updated_at, isActive])

  // Only show strip if task has active ownership or non-ready workflow state
  if (!ownerLabel && !isActive && displayState === 'ready') return null

  return (
    <div className={`task-status-strip ${isActive ? 'task-status-strip--active' : ''} ${compact ? 'task-status-strip--compact' : ''}`}>
      {isActive && <span className="task-status-strip-pulse" />}
      {ownerLabel && <span className="task-status-strip-agent">{ownerLabel}</span>}
      <span className="task-status-strip-step">{stateLabel}</span>
      <span className="task-status-strip-time">{timeLabel}</span>
    </div>
  )
}
