import { useState, useEffect } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { getCanonicalTaskState, getTaskBucket } from '../../lib/taskState'

// =============================================================================
// Activity state classification
// =============================================================================

type ActivityState = 'active' | 'idle' | 'stuck' | 'none'

const ACTIVE_THRESHOLD_MS = 2 * 60 * 1000    // 2 minutes
const IDLE_THRESHOLD_MS = 10 * 60 * 1000      // 10 minutes

function classifyActivity(task: GobbyTask): ActivityState {
  const state = getCanonicalTaskState(task)

  // Only show pulse for in-progress tasks with an assignee
  if (getTaskBucket(task) !== 'in_progress' || !state.owner_session_id) return 'none'

  const elapsed = Date.now() - new Date(task.updated_at).getTime()

  if (elapsed < ACTIVE_THRESHOLD_MS) return 'active'
  if (elapsed < IDLE_THRESHOLD_MS) return 'idle'
  return 'stuck'
}

// =============================================================================
// ActivityPulse component
// =============================================================================

interface ActivityPulseProps {
  task: GobbyTask
  compact?: boolean
}

const STATE_LABELS: Record<ActivityState, string> = {
  active: 'Agent working',
  idle: 'Agent idle',
  stuck: 'Agent may be stuck',
  none: '',
}

export function ActivityPulse({ task, compact }: ActivityPulseProps) {
  const [state, setState] = useState<ActivityState>(() => classifyActivity(task))

  useEffect(() => {
    setState(classifyActivity(task))
    const id = setInterval(() => setState(classifyActivity(task)), 30_000)
    return () => clearInterval(id)
  }, [task])

  if (state === 'none') return null

  return (
    <span
      className={`activity-pulse activity-pulse--${state}`}
      title={STATE_LABELS[state]}
    >
      <span className="activity-pulse-dot" />
      {!compact && (
        <span className="activity-pulse-label">{STATE_LABELS[state]}</span>
      )}
    </span>
  )
}
