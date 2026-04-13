import { useTimeStats } from '../../hooks/useTimeStats'
import { cn } from '../../lib/utils'
import { DashboardCard } from './DashboardCard'
import {
  dashboardCardBodyCenteredClass,
  dashboardDotClass,
  dashboardStatusListClass,
  dashboardStatusRowClass,
  dashboardStatusRowDimmedClass,
  dashboardStatusRowLabelClass,
  dashboardStatusRowValueClass,
} from './dashboardStyles'

type TaskStats = {
  open: number; in_progress: number; closed: number
  needs_review: number; review_approved: number; escalated: number
  ready: number; blocked: number; closed_24h: number
}

const PIE_SEGMENTS: { key: keyof TaskStats; label: string; color: string }[] = [
  { key: 'ready', label: 'Ready', color: '#8b5cf6' },
  { key: 'in_progress', label: 'In Progress', color: '#f59e0b' },
  { key: 'blocked', label: 'Blocked', color: '#ef4444' },
  { key: 'needs_review', label: 'Needs Review', color: '#06b6d4' },
  { key: 'review_approved', label: 'Approved', color: '#10b981' },
  { key: 'escalated', label: 'Escalated', color: '#f97316' },
]

const SIZE = 120
const STROKE = 18
const RADIUS = (SIZE - STROKE) / 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

interface Props {
  hours: number
  projectId?: string
}

export function TasksCard({ hours, projectId }: Props) {
  const { data } = useTimeStats(hours, projectId)

  const tasks = data?.tasks ?? {
    open: 0, in_progress: 0, closed: 0,
    needs_review: 0, review_approved: 0, escalated: 0,
    ready: 0, blocked: 0, closed_24h: 0,
  }

  const openTotal = tasks.ready + tasks.in_progress + tasks.blocked +
    tasks.needs_review + tasks.review_approved + tasks.escalated
  const segments = PIE_SEGMENTS.map(s => ({ ...s, value: tasks[s.key] ?? 0 }))
    .filter(s => s.value > 0)
  const total = segments.reduce((sum, s) => sum + s.value, 0)

  let offset = 0
  const arcs = segments.map(s => {
    const fraction = total > 0 ? s.value / total : 0
    const dashLen = fraction * CIRCUMFERENCE
    const arc = { ...s, dashLen, dashOffset: -offset }
    offset += dashLen
    return arc
  })

  return (
    <DashboardCard title="Tasks" bodyClassName={dashboardCardBodyCenteredClass}>
      <svg width={SIZE} height={SIZE} className="shrink-0">
          {total === 0 ? (
            <circle cx={SIZE / 2} cy={SIZE / 2} r={RADIUS}
              fill="none" stroke="var(--border)" strokeWidth={STROKE} />
          ) : (
            arcs.map(a => (
              <circle key={a.key} cx={SIZE / 2} cy={SIZE / 2} r={RADIUS}
                fill="none" stroke={a.color} strokeWidth={STROKE}
                strokeDasharray={`${a.dashLen} ${CIRCUMFERENCE - a.dashLen}`}
                strokeDashoffset={a.dashOffset}
                transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
              />
            ))
          )}
          <text x={SIZE / 2} y={SIZE / 2 - 6} textAnchor="middle" fill="var(--text-primary)"
            fontSize="22" fontWeight="bold">{openTotal}</text>
          <text x={SIZE / 2} y={SIZE / 2 + 12} textAnchor="middle" fill="var(--text-secondary)"
            fontSize="10">open</text>
      </svg>
      <div className={dashboardStatusListClass}>
        {segments.map(({ key, label, color, value }) => (
          <div key={key} className={dashboardStatusRowClass}>
            <span className={dashboardDotClass} style={{ background: color }} />
            <span className={dashboardStatusRowLabelClass}>{label}</span>
            <span className={dashboardStatusRowValueClass}>{value}</span>
          </div>
        ))}
        {tasks.closed > 0 && (
          <div className={cn(dashboardStatusRowClass, dashboardStatusRowDimmedClass)}>
            <span className={dashboardDotClass} style={{ background: '#737373' }} />
            <span className={dashboardStatusRowLabelClass}>Closed</span>
            <span className={dashboardStatusRowValueClass}>{tasks.closed}</span>
          </div>
        )}
      </div>
    </DashboardCard>
  )
}
