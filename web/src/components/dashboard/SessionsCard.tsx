import { useTimeStats } from '../../hooks/useTimeStats'

const SOURCE_COLORS: Record<string, string> = {
  claude: '#f97316',
  gemini: '#3b82f6',
  codex: '#a855f7',
  pipeline: '#737373',
  cron: '#a3a3a3',
}

const SOURCE_LABELS: Record<string, string> = {
  claude: 'Claude',
  gemini: 'Gemini',
  codex: 'Codex',
  pipeline: 'Pipeline',
  cron: 'Cron',
}

const FALLBACK_COLOR = '#525252'

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
    <div className="dash-card">
      <div className="dash-card-header">
        <h3 className="dash-card-title">Sessions</h3>
      </div>
      <div className="dash-card-body" style={{ display: 'flex', gap: 16, alignItems: 'flex-start' }}>
        <svg width={SIZE} height={SIZE} style={{ flexShrink: 0 }}>
          {total === 0 ? (
            <circle cx={SIZE / 2} cy={SIZE / 2} r={RADIUS}
              fill="none" stroke="#333" strokeWidth={STROKE} />
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
          <text x={SIZE / 2} y={SIZE / 2 - 6} textAnchor="middle" fill="#e5e5e5"
            fontSize="22" fontWeight="bold">{total}</text>
          <text x={SIZE / 2} y={SIZE / 2 + 12} textAnchor="middle" fill="#a3a3a3"
            fontSize="10">total</text>
        </svg>
        <div className="dash-status-list" style={{ flex: 1, minWidth: 0 }}>
          {segments.map(({ key, label, color, value }) => (
            <div key={key} className="dash-status-row">
              <span className="dash-legend-dot" style={{ background: color }} />
              <span className="dash-status-row-label">{label}</span>
              <span className="dash-status-row-value">{value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
