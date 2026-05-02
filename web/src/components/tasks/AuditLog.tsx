import { useMemo, useState } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { useNow } from '../../hooks/useNow'
import { StatusDot } from './TaskBadges'
import { RiskBadge } from './RiskBadges'
import { classifyTaskRisk } from './riskUtils'
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  getTaskStateSummary,
  type TaskDisplayState,
} from '../../lib/taskState'

interface AuditEntry {
  timestamp: string
  action: string
  actor: string
  target: string
  targetId: string
  result: string
  riskLevel: 'critical' | 'high' | 'medium' | 'low' | 'none'
  status: TaskDisplayState
}

type ActionFilter = 'all' | 'created' | 'closed' | 'status_change' | 'high_risk'
type TimeFilter = 'all' | '1h' | '24h' | '7d'

const ROOT_CLS = 'flex flex-1 flex-col overflow-hidden'
const FILTERS_CLS =
  'flex shrink-0 flex-wrap items-center gap-3 border-b border-[var(--border)] px-3 py-2'
const FILTER_GROUP_CLS = 'flex items-center gap-1'
const FILTER_LABEL_CLS = 'mr-[0.15rem] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const FILTER_BTN_CLS =
  'cursor-pointer rounded-[0.2rem] border border-[var(--border)] bg-[var(--bg-primary)] px-[0.4rem] py-[0.15rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-secondary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const FILTER_BTN_ACTIVE_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:bg-[var(--accent)]'
const COUNT_CLS = 'ml-auto font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const ENTRIES_CLS = 'flex-1 overflow-y-auto py-1'
const EMPTY_CLS =
  'p-8 text-center text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]'
const ENTRY_CLS =
  'flex w-full cursor-pointer items-center gap-[0.4rem] border-0 border-b border-[var(--border)] bg-transparent px-3 py-[0.35rem] text-left font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)]'
const ENTRY_FAILURE_CLS =
  'bg-[color-mix(in_srgb,var(--color-error)_4%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)]'
const ENTRY_TIME_CLS =
  'min-w-[5.5rem] shrink-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)]'
const ENTRY_ICON_CLS =
  'w-4 shrink-0 text-center text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const ENTRY_ACTION_CLS =
  'min-w-[5.5rem] shrink-0 text-[length:calc(var(--font-size-base)*0.7)] font-semibold text-[var(--text-secondary)]'
const ENTRY_TARGET_CLS = 'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap'
const ENTRY_ACTOR_CLS =
  'shrink-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)]'

function deriveAuditEntries(tasks: GobbyTask[]): AuditEntry[] {
  const entries: AuditEntry[] = []

  for (const task of tasks) {
    const risk = classifyTaskRisk(task.title, task.task_type)
    const displayState = getTaskDisplayState(task)

    entries.push({
      timestamp: task.created_at,
      action: 'created',
      actor: getCanonicalTaskState(task).owner_session_id || 'system',
      target: `${task.ref} ${task.title}`,
      targetId: task.id,
      result: 'success',
      riskLevel: risk,
      status: displayState,
    })

    if (displayState !== 'ready') {
      entries.push({
        timestamp: task.updated_at,
        action: displayState === 'closed' ? 'closed' : 'status_change',
        actor: getCanonicalTaskState(task).owner_session_id || 'system',
        target: `${task.ref} → ${getTaskStateSummary(task)}`,
        targetId: task.id,
        result: displayState === 'blocked' ? 'failure' : 'success',
        riskLevel: risk,
        status: displayState,
      })
    }
  }

  entries.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())

  return entries
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso)
  const now = new Date()
  const diffMs = now.getTime() - d.getTime()
  const diffMin = Math.floor(diffMs / 60000)

  if (diffMin < 1) return 'just now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHrs = Math.floor(diffMin / 60)
  if (diffHrs < 24) return `${diffHrs}h ago`

  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function timeFilterMs(filter: TimeFilter): number {
  switch (filter) {
    case '1h': return 60 * 60 * 1000
    case '24h': return 24 * 60 * 60 * 1000
    case '7d': return 7 * 24 * 60 * 60 * 1000
    default: return Infinity
  }
}

const ACTION_ICONS: Record<string, string> = {
  created: '+',
  closed: '✓',
  status_change: '→',
}

const ACTION_LABELS: Record<string, string> = {
  created: 'Created',
  closed: 'Closed',
  status_change: 'Status Changed',
}

interface AuditLogProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
}

export function AuditLog({ tasks, onSelectTask }: AuditLogProps) {
  const [actionFilter, setActionFilter] = useState<ActionFilter>('all')
  const [timeFilter, setTimeFilter] = useState<TimeFilter>('all')
  const now = useNow()

  const allEntries = useMemo(() => deriveAuditEntries(tasks), [tasks])

  const filtered = useMemo(() => {
    const cutoff = now - timeFilterMs(timeFilter)

    return allEntries.filter(entry => {
      if (timeFilter !== 'all' && new Date(entry.timestamp).getTime() < cutoff) return false

      if (actionFilter === 'high_risk') {
        return entry.riskLevel === 'critical' || entry.riskLevel === 'high'
      }
      if (actionFilter !== 'all' && entry.action !== actionFilter) return false

      return true
    })
  }, [allEntries, actionFilter, timeFilter, now])

  return (
    <div className={ROOT_CLS}>
      <div className={FILTERS_CLS}>
        <div className={FILTER_GROUP_CLS}>
          <span className={FILTER_LABEL_CLS}>Action:</span>
          {(['all', 'created', 'closed', 'status_change', 'high_risk'] as const).map(f => (
            <button
              key={f}
              className={actionFilter === f ? `${FILTER_BTN_CLS} ${FILTER_BTN_ACTIVE_CLS}` : FILTER_BTN_CLS}
              onClick={() => setActionFilter(f)}
            >
              {f === 'all' ? 'All' : f === 'high_risk' ? 'High Risk' : ACTION_LABELS[f] || f}
            </button>
          ))}
        </div>
        <div className={FILTER_GROUP_CLS}>
          <span className={FILTER_LABEL_CLS}>Time:</span>
          {(['all', '1h', '24h', '7d'] as const).map(f => (
            <button
              key={f}
              className={timeFilter === f ? `${FILTER_BTN_CLS} ${FILTER_BTN_ACTIVE_CLS}` : FILTER_BTN_CLS}
              onClick={() => setTimeFilter(f)}
            >
              {f === 'all' ? 'All time' : f}
            </button>
          ))}
        </div>
        <span className={COUNT_CLS}>{filtered.length} entries</span>
      </div>

      <div className={ENTRIES_CLS}>
        {filtered.length === 0 ? (
          <div className={EMPTY_CLS}>No audit entries match filters</div>
        ) : (
          filtered.map((entry, i) => (
            <button
              key={`${entry.targetId}-${entry.action}-${i}`}
              className={entry.result === 'failure' ? `${ENTRY_CLS} ${ENTRY_FAILURE_CLS}` : ENTRY_CLS}
              onClick={() => onSelectTask(entry.targetId)}
            >
              <span className={ENTRY_TIME_CLS}>{formatTimestamp(entry.timestamp)}</span>
              <span className={ENTRY_ICON_CLS}>{ACTION_ICONS[entry.action] || '•'}</span>
              <StatusDot status={entry.status} />
              <span className={ENTRY_ACTION_CLS}>{ACTION_LABELS[entry.action] || entry.action}</span>
              <span className={ENTRY_TARGET_CLS}>{entry.target}</span>
              <RiskBadge level={entry.riskLevel} compact />
              <span className={ENTRY_ACTOR_CLS} title={entry.actor}>
                {entry.actor === 'system' ? 'system' : entry.actor.length > 8 ? entry.actor.slice(0, 8) + '…' : entry.actor}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
