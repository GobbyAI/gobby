import { useTimeStats } from '../../hooks/useTimeStats'
import { SOURCE_COLORS, SOURCE_LABELS } from '../shared/sourceTheme'
import { DashboardCard } from './DashboardCard'
import {
  dashboardCardBodyRowClass,
  dashboardDotClass,
  dashboardStatusListClass,
  dashboardStatusRowClass,
  dashboardStatusRowLabelClass,
  dashboardStatusRowValueClass,
} from './dashboardStyles'

const FALLBACK_COLOR = SOURCE_COLORS.default

const SIZE = 120
const STROKE = 18
const RADIUS = (SIZE - STROKE) / 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

interface Props {
  hours: number
  projectId?: string
}

export function SessionsCard({ hours, projectId }: Props) {
  const { data } = useTimeStats(hours, projectId)

  const sessions = data?.sessions ?? { active: 0, paused: 0, handoff_ready: 0, total: 0, by_source: {} }
  const bySource = sessions.by_source ?? {}

  // Build segments from source data, filter out zeros
  const segments = Object.entries(bySource)
    .map(([src, statuses]) => ({
      key: src,
      label: SOURCE_LABELS[src] ?? src,
      color: SOURCE_COLORS[src] ?? FALLBACK_COLOR,
      value: Object.values(statuses).reduce((sum, n) => sum + n, 0),
    }))
    .filter(s => s.value > 0)
    .sort((a, b) => b.value - a.value)

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
    <DashboardCard title="Sessions" bodyClassName={dashboardCardBodyRowClass}>
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
            fontSize="22" fontWeight="bold">{total}</text>
          <text x={SIZE / 2} y={SIZE / 2 + 12} textAnchor="middle" fill="var(--text-secondary)"
            fontSize="10">total</text>
      </svg>
      <div className={dashboardStatusListClass}>
        {segments.map(({ key, label, color, value }) => (
          <div key={key} className={dashboardStatusRowClass}>
            <span className={dashboardDotClass} style={{ background: color }} />
            <span className={dashboardStatusRowLabelClass}>{label}</span>
            <span className={dashboardStatusRowValueClass}>{value}</span>
          </div>
        ))}
      </div>
    </DashboardCard>
  )
}
