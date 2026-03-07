import { useMemo, useState } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot } from './TaskBadges'
import { classifyTaskRisk, RiskBadge } from './RiskBadges'

// =============================================================================
// Types
// =============================================================================

interface AuditEntry {
  timestamp: string
  action: string
  actor: string
  target: string
  targetId: string
  result: string
  riskLevel: 'critical' | 'high' | 'medium' | 'low' | 'none'
  status: string
}

type ActionFilter = 'all' | 'created' | 'closed' | 'status_change' | 'high_risk'
type TimeFilter = 'all' | '1h' | '24h' | '7d'

// =============================================================================
// Derive audit entries from tasks
// =============================================================================

function deriveAuditEntries(tasks: GobbyTask[]): AuditEntry[] {
  const entries: AuditEntry[] = []

  for (const task of tasks) {
    const risk = classifyTaskRisk(task.title, task.task_type)

    // Task created
    entries.push({
      timestamp: task.created_at,
      action: 'created',
      actor: task.assignee || 'system',
      target: `${task.ref} ${task.title}`,
      targetId: task.id,
      result: 'success',
      riskLevel: risk,
      status: task.status,
    })

    // Task status (if not open, it changed status)
    if (task.status !== 'open') {
      entries.push({
        timestamp: task.updated_at,
        action: task.status === 'closed' ? 'closed' : 'status_change',
        actor: task.assignee || 'system',
        target: `${task.ref} → ${task.status.replace(/_/g, ' ')}`,
        targetId: task.id,
        result: task.status === 'escalated' ? 'failure' : 'success',
        riskLevel: risk,
        status: task.status,
      })
    }
  }

  // Sort by timestamp descending (most recent first)
  entries.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())

  return entries
}

// =============================================================================
// Helpers
// =============================================================================

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

// =============================================================================
// AuditLog
// =============================================================================

interface AuditLogProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
}

export function AuditLog({ tasks, onSelectTask }: AuditLogProps) {
  const [actionFilter, setActionFilter] = useState<ActionFilter>('all')
  const [timeFilter, setTimeFilter] = useState<TimeFilter>('all')

  const allEntries = useMemo(() => deriveAuditEntries(tasks), [tasks])

  const filtered = useMemo(() => {
    const now = Date.now()
    const cutoff = now - timeFilterMs(timeFilter)

    return allEntries.filter(entry => {
      // Time filter
      if (timeFilter !== 'all' && new Date(entry.timestamp).getTime() < cutoff) return false

      // Action filter
      if (actionFilter === 'high_risk') {
        return entry.riskLevel === 'critical' || entry.riskLevel === 'high'
      }
      if (actionFilter !== 'all' && entry.action !== actionFilter) return false

      return true
    })
  }, [allEntries, actionFilter, timeFilter])

  return (
    <div className="audit-log">
      {/* Filter bar */}
      <div className="audit-log-filters">
        <div className="audit-log-filter-group">
          <span className="audit-log-filter-label">Action:</span>
          {(['all', 'created', 'closed', 'status_change', 'high_risk'] as const).map(f => (
            <button
              key={f}
              className={`audit-log-filter-btn ${actionFilter === f ? 'active' : ''}`}
              onClick={() => setActionFilter(f)}
            >
              {f === 'all' ? 'All' : f === 'high_risk' ? 'High Risk' : ACTION_LABELS[f] || f}
            </button>
          ))}
        </div>
        <div className="audit-log-filter-group">
          <span className="audit-log-filter-label">Time:</span>
          {(['all', '1h', '24h', '7d'] as const).map(f => (
            <button
              key={f}
              className={`audit-log-filter-btn ${timeFilter === f ? 'active' : ''}`}
              onClick={() => setTimeFilter(f)}
            >
              {f === 'all' ? 'All time' : f}
            </button>
          ))}
        </div>
        <span className="audit-log-count">{filtered.length} entries</span>
      </div>

      {/* Log entries */}
      <div className="audit-log-entries">
        {filtered.length === 0 ? (
          <div className="audit-log-empty">No audit entries match filters</div>
        ) : (
          filtered.map((entry, i) => (
            <button
              key={`${entry.targetId}-${entry.action}-${i}`}
              className={`audit-log-entry ${entry.result === 'failure' ? 'audit-log-entry--failure' : ''}`}
              onClick={() => onSelectTask(entry.targetId)}
            >
              <span className="audit-log-entry-time">{formatTimestamp(entry.timestamp)}</span>
              <span className="audit-log-entry-icon">{ACTION_ICONS[entry.action] || '•'}</span>
              <StatusDot status={entry.status} />
              <span className="audit-log-entry-action">{ACTION_LABELS[entry.action] || entry.action}</span>
              <span className="audit-log-entry-target">{entry.target}</span>
              <RiskBadge level={entry.riskLevel} compact />
              <span className="audit-log-entry-actor" title={entry.actor}>
                {entry.actor === 'system' ? 'system' : entry.actor.length > 8 ? entry.actor.slice(0, 8) + '\u2026' : entry.actor}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  )
}
