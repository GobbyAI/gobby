import { useState, useEffect } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { getCanonicalTaskState, getTaskBucket } from '../../lib/taskState'
import { cn } from '../../lib/utils'

type ActivityState = 'active' | 'idle' | 'stuck' | 'none'

const ACTIVE_THRESHOLD_MS = 2 * 60 * 1000
const IDLE_THRESHOLD_MS = 10 * 60 * 1000

const ROOT_CLS = 'inline-flex items-center gap-1'
const DOT_CLS = 'h-[7px] w-[7px] shrink-0 rounded-full [animation:pulse-glow_1.8s_ease-in-out_infinite]'
const DOT_ACTIVE_CLS =
  'bg-[var(--color-success-foreground)] [box-shadow:0_0_4px_color-mix(in_srgb,var(--color-success-foreground)_60%,transparent)]'
const DOT_IDLE_CLS =
  'bg-[var(--color-warning-foreground)] [animation-duration:2.4s] [box-shadow:0_0_4px_color-mix(in_srgb,var(--color-warning-foreground)_50%,transparent)]'
const DOT_STUCK_CLS =
  'bg-[var(--color-error)] [animation-duration:3s] [box-shadow:0_0_4px_color-mix(in_srgb,var(--color-error)_50%,transparent)]'
const LABEL_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

function classifyActivity(task: GobbyTask): ActivityState {
  const state = getCanonicalTaskState(task)

  if (getTaskBucket(task) !== 'in_progress' || !state.owner_session_id) return 'none'

  const elapsed = Date.now() - new Date(task.updated_at).getTime()

  if (elapsed < ACTIVE_THRESHOLD_MS) return 'active'
  if (elapsed < IDLE_THRESHOLD_MS) return 'idle'
  return 'stuck'
}

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

const DOT_STATE_CLS: Record<Exclude<ActivityState, 'none'>, string> = {
  active: DOT_ACTIVE_CLS,
  idle: DOT_IDLE_CLS,
  stuck: DOT_STUCK_CLS,
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
    <span className={ROOT_CLS} title={STATE_LABELS[state]}>
      <span className={cn(DOT_CLS, DOT_STATE_CLS[state])} />
      {!compact && (
        <span className={LABEL_CLS}>{STATE_LABELS[state]}</span>
      )}
    </span>
  )
}
