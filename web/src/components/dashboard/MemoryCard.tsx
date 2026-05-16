import { useTimeStats } from '../../hooks/useTimeStats'
import { chartSeriesAt } from '../../lib/chartSeries'
import { donutArcs } from '../../lib/donutArc'
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

  const arcs = donutArcs(segments, SIZE / 2, SIZE / 2, RADIUS)

  return (
    <DashboardCard title="Memory" bodyClassName={dashboardCardBodyRowClass}>
      <svg width={SIZE} height={SIZE} className="shrink-0">
          {total === 0 ? (
            <circle cx={SIZE / 2} cy={SIZE / 2} r={RADIUS}
              fill="none" stroke="var(--border)" strokeWidth={STROKE} />
          ) : (
            arcs.map(({ segment, pathD }, i) => (
              <path key={segment.key} d={pathD}
                fill="none" stroke={segment.color} strokeWidth={STROKE}
                strokeDasharray={chartSeriesAt(i).dash}
                strokeLinecap="butt"
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
