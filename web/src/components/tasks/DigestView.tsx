import { useState, useMemo } from 'react'
import type { GobbyTask } from '../../hooks/useTasks'
import { StatusDot, PriorityBadge, TypeBadge } from './TaskBadges'
import { relativeTime } from '../../utils/formatTime'
import { getTaskDisplayState } from '../../lib/taskState'
import { cn } from '../../lib/utils'

type TimePeriod = 'today' | 'week' | 'all'

interface DigestSection {
  key: string
  title: string
  icon: string
  tasks: GobbyTask[]
  color: string
}

const ROOT_CLS = 'flex flex-col gap-3'
const TOOLBAR_CLS = 'flex items-center gap-1.5'
const TOOLBAR_LABEL_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'
const PERIOD_BTN_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] pointer-coarse:min-h-11'
const PERIOD_BTN_ACTIVE_CLS =
  'border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]'
const SUMMARY_CLS = 'ml-auto text-[length:var(--text-sm)] text-[var(--text-muted)]'

const SECTIONS_CLS = 'flex flex-col gap-2'
const SECTION_CLS = 'overflow-hidden rounded-md border border-[var(--border)]'
const SECTION_HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-2 border-0 bg-[var(--bg-secondary)] px-3 py-2.5 text-left font-[inherit] text-[length:var(--text-md)] font-semibold text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const SECTION_ICON_CLS = 'text-[length:var(--text-base)]'
const SECTION_TITLE_CLS = 'flex-1'
const SECTION_COUNT_CLS = 'rounded-[10px] px-2 py-px text-[length:var(--text-xs)] font-semibold'
const SECTION_CHEVRON_CLS = 'text-[length:var(--text-xs)] text-[var(--text-muted)]'

const SECTION_ITEMS_CLS = 'flex flex-col'
const ITEM_CLS =
  'flex w-full cursor-pointer items-center gap-2 border-0 border-t border-[var(--border)] bg-transparent px-3 py-2 text-left font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] pointer-coarse:min-h-11'
const ITEM_REF_CLS = 'min-w-12 font-[inherit] text-[length:var(--text-xs)] text-[var(--text-muted)]'
const ITEM_TITLE_CLS = 'flex-1 overflow-hidden text-ellipsis whitespace-nowrap'
const ITEM_META_CLS = 'flex shrink-0 items-center gap-1.5'
const ITEM_TIME_CLS = 'font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'

const EMPTY_CLS = 'p-8 text-center text-[length:var(--text-md)] text-[var(--text-muted)]'

function startOfToday(): Date {
  const d = new Date()
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

function startOfWeek(): Date {
  const d = startOfToday()
  const day = d.getDay()
  d.setDate(d.getDate() - (day === 0 ? 6 : day - 1))
  return d
}

function isAfter(iso: string, cutoff: Date): boolean {
  return new Date(iso).getTime() >= cutoff.getTime()
}

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
    return new Date(0)
  }, [period])

  const sections = useMemo((): DigestSection[] => {
    const completed = tasks.filter(
      t => getTaskDisplayState(t) === 'closed' && isAfter(t.updated_at, cutoff)
    )

    const inProgress = tasks.filter(t => getTaskDisplayState(t) === 'in_progress')

    const review = tasks.filter(t => getTaskDisplayState(t) === 'needs_review')
    const approved = tasks.filter(t => getTaskDisplayState(t) === 'review_approved')
    const blocked = tasks.filter(t => getTaskDisplayState(t) === 'blocked')

    const newTasks = tasks.filter(
      t => getTaskDisplayState(t) === 'ready' && isAfter(t.created_at, cutoff)
    )

    return [
      { key: 'blocked', title: 'Blocked', icon: '⚠', tasks: blocked, color: 'var(--color-warning-foreground)' },
      { key: 'review', title: 'In Review', icon: '\u{1F50D}', tasks: review, color: 'var(--color-agent)' },
      { key: 'approved', title: 'Review Approved', icon: '\u{1F9F7}', tasks: approved, color: 'var(--color-review)' },
      { key: 'in-progress', title: 'In Progress', icon: '\u{1F504}', tasks: inProgress, color: 'var(--color-info)' },
      {
        key: 'completed',
        title: period === 'today' ? 'Completed Today' : period === 'week' ? 'Completed This Week' : 'All Completed',
        icon: '✅',
        tasks: completed,
        color: 'var(--color-success-foreground)',
      },
      {
        key: 'new',
        title: period === 'today' ? 'Created Today' : period === 'week' ? 'Created This Week' : 'All Open',
        icon: '\u{1F195}',
        tasks: newTasks,
        color: 'var(--text-muted)',
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
    <div className={ROOT_CLS}>
      <div className={TOOLBAR_CLS}>
        <span className={TOOLBAR_LABEL_CLS}>Period:</span>
        {(['today', 'week', 'all'] as const).map(p => (
          <button
            key={p}
            className={cn(PERIOD_BTN_CLS, period === p && PERIOD_BTN_ACTIVE_CLS)}
            onClick={() => setPeriod(p)}
          >
            {p === 'today' ? 'Today' : p === 'week' ? 'This Week' : 'All Time'}
          </button>
        ))}
        <span className={SUMMARY_CLS}>{totalActive} tasks</span>
      </div>

      <div className={SECTIONS_CLS}>
        {sections.map(section => {
          if (section.tasks.length === 0) return null
          const isCollapsed = collapsedSections.has(section.key)

          return (
            <div key={section.key} className={SECTION_CLS}>
              <button
                className={SECTION_HEADER_CLS}
                onClick={() => toggleSection(section.key)}
              >
                <span className={SECTION_ICON_CLS}>{section.icon}</span>
                <span className={SECTION_TITLE_CLS}>{section.title}</span>
                <span
                  className={SECTION_COUNT_CLS}
                  style={{ background: `color-mix(in srgb, ${section.color} 12%, transparent)`, color: section.color }}
                >
                  {section.tasks.length}
                </span>
                <span className={SECTION_CHEVRON_CLS}>{isCollapsed ? '▸' : '▾'}</span>
              </button>

              {!isCollapsed && (
                <div className={SECTION_ITEMS_CLS}>
                  {section.tasks.map(task => (
                    <button
                      key={task.id}
                      className={ITEM_CLS}
                      onClick={() => onSelectTask(task.id)}
                    >
                      <StatusDot task={task} />
                      <span className={ITEM_REF_CLS}>{task.ref}</span>
                      <span className={ITEM_TITLE_CLS}>{task.title}</span>
                      <span className={ITEM_META_CLS}>
                        <PriorityBadge priority={task.priority} />
                        <TypeBadge type={task.task_type} />
                        <span className={ITEM_TIME_CLS}>{relativeTime(task.updated_at)}</span>
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )
        })}

        {sections.every(s => s.tasks.length === 0) && (
          <div className={EMPTY_CLS}>No activity for this period</div>
        )}
      </div>
    </div>
  )
}
