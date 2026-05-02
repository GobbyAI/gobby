import type { ProjectWithStats } from '../../hooks/useProjects'
import { cn } from '../../lib/utils'

const OVERVIEW_CLS = 'flex shrink-0 gap-3 py-3'
const CARD_CLS =
  'flex flex-1 cursor-pointer flex-col items-center gap-1 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3 transition-colors duration-150 hover:border-[var(--text-muted)]'
const CARD_ACTIVE_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-agent)_8%,transparent)]'
const COUNT_CLS = 'font-[inherit] text-[length:var(--text-3xl)] font-semibold'
const LABEL_CLS =
  'text-[length:var(--text-xs)] uppercase tracking-[0.04em] text-[var(--text-muted)]'

const COUNT_COLOR_BY_KEY: Record<string, string> = {
  total: 'text-[var(--accent)]',
  active: 'text-[var(--color-success-foreground)]',
  tasks: 'text-[var(--color-warning-foreground)]',
}

interface ProjectOverviewProps {
  projects: ProjectWithStats[]
  totalSessions: number
  totalOpenTasks: number
  activeFilter: string | null
  onFilter: (filter: string | null) => void
}

export function ProjectOverview({ projects, totalSessions, totalOpenTasks, activeFilter, onFilter }: ProjectOverviewProps) {
  const cards = [
    { key: 'total', label: 'Projects', count: projects.length },
    { key: 'active', label: 'Active Sessions', count: totalSessions },
    { key: 'tasks', label: 'Open Tasks', count: totalOpenTasks },
  ]

  return (
    <div className={OVERVIEW_CLS}>
      {cards.map(card => (
        <button
          key={card.key}
          className={cn(CARD_CLS, activeFilter === card.key && CARD_ACTIVE_CLS)}
          onClick={() => onFilter(activeFilter === card.key ? null : card.key)}
        >
          <span className={cn(COUNT_CLS, COUNT_COLOR_BY_KEY[card.key] ?? '')}>{card.count}</span>
          <span className={LABEL_CLS}>{card.label}</span>
        </button>
      ))}
    </div>
  )
}
