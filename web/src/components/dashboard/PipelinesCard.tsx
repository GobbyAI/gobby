type SegmentKey = 'running' | 'waiting_approval' | 'completed' | 'failed'

type PipelineCounts = Record<SegmentKey, number> & { total: number }

interface Props {
  pipelines: PipelineCounts
}

const SEGMENTS: readonly { key: SegmentKey; label: string; color: string }[] = [
  { key: 'running', label: 'Running', color: '#3b82f6' },
  { key: 'waiting_approval', label: 'Waiting', color: '#f59e0b' },
  { key: 'completed', label: 'Completed', color: '#22c55e' },
  { key: 'failed', label: 'Failed', color: '#ef4444' },
]

const RADIUS = 36
const STROKE = 8
const SIZE = (RADIUS + STROKE) * 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

export function PipelinesCard({ pipelines }: Props) {
  const total = pipelines.total

  let offset = 0
  const rings = SEGMENTS.map(({ key, color }) => {
    const value = pipelines[key]
    const ratio = total > 0 ? value / total : 0
    const length = ratio * CIRCUMFERENCE
    const ring = { key, color, length, offset }
    offset += length
    return ring
  })

  return (
    <div className="dash-card">
      <div className="dash-card-header">
        <h3 className="dash-card-title">Pipelines</h3>
      </div>
      <div className="dash-card-body">
        <div className="dash-donut-container">
          <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="Pipeline status chart">
            <circle
              cx={SIZE / 2}
              cy={SIZE / 2}
              r={RADIUS}
              fill="none"
              stroke="var(--border-color)"
              strokeWidth={STROKE}
            />
            {rings.map((ring) =>
              ring.length > 0 ? (
                <circle
                  key={ring.key}
                  cx={SIZE / 2}
                  cy={SIZE / 2}
                  r={RADIUS}
                  fill="none"
                  stroke={ring.color}
                  strokeWidth={STROKE}
                  strokeDasharray={`${ring.length} ${CIRCUMFERENCE - ring.length}`}
                  strokeDashoffset={-ring.offset}
                  transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
                />
              ) : null
            )}
            <text
              x={SIZE / 2}
              y={SIZE / 2}
              textAnchor="middle"
              dominantBaseline="central"
              fill="var(--text-primary)"
              fontSize="16"
              fontWeight="600"
              fontFamily="var(--font-mono)"
            >
              {total}
            </text>
          </svg>
          <div className="dash-donut-legend">
            {SEGMENTS.map(({ key, label, color }) => (
              <div key={key} className="dash-legend-item">
                <span className="dash-legend-dot" style={{ background: color }} />
                <span className="dash-legend-label">{label}</span>
                <span className="dash-legend-value">{pipelines[key]}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
