import { useState, useMemo } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { relativeTime } from '../../utils/formatTime'
import { getTaskBucket } from '../../lib/taskState'

// =============================================================================
// Types
// =============================================================================

type TimePeriod = 'today' | 'week' | 'all'

interface DigestSection {
  key: string
  title: string
  icon: string
  tasks: GobbyTask[]
  color: string
}

// =============================================================================
// Helpers
// =============================================================================

function startOfToday(): Date {
  const d = new Date()
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

function startOfWeek(): Date {
  const d = startOfToday()
  const day = d.getDay()
  d.setDate(d.getDate() - (day === 0 ? 6 : day - 1)) // Monday start
  return d
}

function isAfter(iso: string, cutoff: Date): boolean {
  return new Date(iso).getTime() >= cutoff.getTime()
}

// =============================================================================
// DigestView
// =============================================================================

interface DigestViewProps {
  tasks: GobbyTask[]
  onSelectTask: (id: string) => void
}

export function DigestView({ tasks, onSelectTask }: DigestViewProps) {
  const [period, setPeriod] = useState<TimePeriod>('today')
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set())

  const cutoff = useMemo(() => {
    if (period === 'today') return startOfToday()
    if (period === 'week') return startOfWeek()
    return new Date(0) // all time
  }, [period])

  const sections = useMemo((): DigestSection[] => {
    // Completed: closed tasks within period
    const completed = tasks.filter(
      t => getTaskBucket(t) === 'closed' && isAfter(t.updated_at, cutoff)
    )

    const inProgress = tasks.filter(t => getTaskBucket(t) === 'in_progress')

    const review = tasks.filter(t => getTaskBucket(t) === 'review')
    const mergeReady = tasks.filter(t => getTaskBucket(t) === 'merge_ready')
    const blocked = tasks.filter(t => getTaskBucket(t) === 'blocked')

    const newTasks = tasks.filter(
      t => getTaskBucket(t) === 'ready' && isAfter(t.created_at, cutoff)
    )

    return [
      {
        key: 'blocked',
        title: 'Blocked',
        icon: '\u26A0',
        tasks: blocked,
        color: '#f59e0b',
      },
      {
        key: 'review',
        title: 'In Review',
        icon: '\u{1F50D}',
        tasks: review,
        color: '#a855f7',
      },
      {
        key: 'merge-ready',
        title: 'Merge Ready',
        icon: '\u{1F9F7}',
        tasks: mergeReady,
        color: '#14b8a6',
      },
      {
        key: 'in-progress',
        title: 'In Progress',
        icon: '\u{1F504}',
        tasks: inProgress,
        color: '#3b82f6',
      },
      {
        key: 'completed',
        title: period === 'today' ? 'Completed Today' : period === 'week' ? 'Completed This Week' : 'All Completed',
        icon: '\u2705',
        tasks: completed,
        color: '#22c55e',
      },
      {
        key: 'new',
        title: period === 'today' ? 'Created Today' : period === 'week' ? 'Created This Week' : 'All Open',
        icon: '\u{1F195}',
        tasks: newTasks,
        color: '#737373',
      },
    ]
  }, [tasks, cutoff, period])

  const toggleSection = (key: string) => {
    setCollapsedSections(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const totalActive = sections.reduce((sum, s) => sum + s.tasks.length, 0)

  return (
    <div className="digest-view">
      {/* Period toggle */}
      <div className="digest-toolbar">
        <span className="digest-toolbar-label">Period:</span>
        {(['today', 'week', 'all'] as const).map(p => (
          <button
            key={p}
            className={`digest-period-btn ${period === p ? 'active' : ''}`}
            onClick={() => setPeriod(p)}
          >
            {p === 'today' ? 'Today' : p === 'week' ? 'This Week' : 'All Time'}
          </button>
        ))}
        <span className="digest-summary">{totalActive} tasks</span>
      </div>

      {/* Sections */}
      <div className="digest-sections">
        {sections.map(section => {
          if (section.tasks.length === 0) return null
          const isCollapsed = collapsedSections.has(section.key)

          return (
            <div key={section.key} className="digest-section">
              <button
                className="digest-section-header"
                onClick={() => toggleSection(section.key)}
              >
                <span className="digest-section-icon">{section.icon}</span>
                <span className="digest-section-title">{section.title}</span>
                <span
                  className="digest-section-count"
                  style={{ background: section.color + '20', color: section.color }}
                >
                  {section.tasks.length}
                </span>
                <span className="digest-section-chevron">{isCollapsed ? '\u25B8' : '\u25BE'}</span>
              </button>

              {!isCollapsed && (
                <div className="digest-section-items">
                  {section.tasks.map(task => (
                    <button
                      key={task.id}
                      className="digest-item"
                      onClick={() => onSelectTask(task.id)}
                    >
                      <StatusDot task={task} />
                      <span className="digest-item-ref">{task.ref}</span>
                      <span className="digest-item-title">{task.title}</span>
                      <span className="digest-item-meta">
                        <PriorityBadge priority={task.priority} />
                        <TypeBadge type={task.task_type} />
                        <span className="digest-item-time">{relativeTime(task.updated_at)}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}

        {sections.every(s => s.tasks.length === 0) && (
          <div className="digest-empty">No activity for this period</div>
        )}
      </div>
    </div>
  )
}
