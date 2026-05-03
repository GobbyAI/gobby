import { useTimeStats } from '../../hooks/useTimeStats'
import { cn } from '../../lib/utils'
import { DashboardCard } from './DashboardCard'
import {
  dashboardCardBodyRowClass,
  dashboardDotClass,
  dashboardStatusListClass,
  dashboardStatusRowClass,
  dashboardStatusRowDimmedClass,
  dashboardStatusRowLabelClass,
  dashboardStatusRowValueClass,
} from './dashboardStyles'

const TYPE_COLORS: Record<string, { label: string; color: string }> = {
  fact: { label: 'Facts', color: 'var(--color-info)' },
  preference: { label: 'Preferences', color: 'var(--accent)' },
  pattern: { label: 'Patterns', color: 'var(--color-warning-foreground)' },
  context: { label: 'Context', color: 'var(--color-success-foreground)' },
}

const FALLBACK_COLOR = 'var(--text-muted)'
const MAX_CATEGORIES = 5

const SIZE = 120
const STROKE = 18
const RADIUS = (SIZE - STROKE) / 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

interface Props {
  hours: number
  projectId?: string
}

export function MemoryCard({ hours, projectId }: Props) {
  const { data, isLoading, error } = useTimeStats(hours, projectId)

  if (isLoading) {
    return (
      <DashboardCard title="Memory">
        <p className="text-xs text-muted-foreground">Loading...</p>
      </DashboardCard>
    )
  }

  if (error) {
    return (
      <DashboardCard title="Memory">
        <p className="text-xs text-muted-foreground">Failed to load memory data</p>
      </DashboardCard>
    )
  }

  const memory = data?.memory ?? { count: 0, by_type: {}, recent_count: 0 }

  const byType = memory.by_type ?? {}
  const allSegments = Object.entries(byType)
    .map(([type, count]) => {
      const meta = TYPE_COLORS[type] ?? { label: type, color: FALLBACK_COLOR }
      return { key: type, label: meta.label, color: meta.color, value: count }
    })
    .filter(s => s.value > 0)
    .sort((a, b) => b.value - a.value)

  // Show top 5, collapse rest into "Other"
  const top = allSegments.slice(0, MAX_CATEGORIES)
  const restValue = allSegments.slice(MAX_CATEGORIES).reduce((sum, s) => sum + s.value, 0)
  const segments = restValue > 0
    ? [...top, { key: '_other', label: 'Other', color: FALLBACK_COLOR, value: restValue }]
    : top

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
    <DashboardCard title="Memory" bodyClassName={dashboardCardBodyRowClass}>
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
            fontSize="22" fontWeight="bold">{memory.count}</text>
          <text x={SIZE / 2} y={SIZE / 2 + 12} textAnchor="middle" fill="var(--text-secondary)"
            fontSize="10">total</text>
      </svg>
      <div className={dashboardStatusListClass}>
        {segments.map(({ key, label, color, value }) => (
          <div
            key={key}
            className={cn(
              dashboardStatusRowClass,
              key === '_other' && dashboardStatusRowDimmedClass,
            )}
          >
            <span className={dashboardDotClass} style={{ background: color }} />
            <span className={dashboardStatusRowLabelClass}>{label}</span>
            <span className={dashboardStatusRowValueClass}>{value}</span>
          </div>
        ))}
      </div>
    </DashboardCard>
  )
}
