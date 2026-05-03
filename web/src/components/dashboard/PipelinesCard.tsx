import { DashboardCard } from './DashboardCard'
import {
  dashboardDonutLayoutClass,
  dashboardDotClass,
  dashboardLegendClass,
  dashboardLegendLabelClass,
  dashboardLegendRowClass,
  dashboardLegendValueClass,
} from './dashboardStyles'

type SegmentKey = 'running' | 'waiting_approval' | 'completed' | 'failed'

type PipelineCounts = Record<SegmentKey, number> & { total: number }

interface Props {
  pipelines: PipelineCounts
}

const SEGMENTS: readonly { key: SegmentKey; label: string; color: string }[] = [
  { key: 'running', label: 'Running', color: 'var(--color-info)' },
  { key: 'waiting_approval', label: 'Waiting', color: 'var(--color-warning-foreground)' },
  { key: 'completed', label: 'Completed', color: 'var(--color-success-foreground)' },
  { key: 'failed', label: 'Failed', color: 'var(--color-error)' },
]

const RADIUS = 36
const STROKE = 8
const SIZE = (RADIUS + STROKE) * 2
const CIRCUMFERENCE = 2 * Math.PI * RADIUS

export function PipelinesCard({ pipelines }: Props) {
  const total = pipelines.total

  const rings = SEGMENTS.map(({ key, color }, index) => {
    const value = pipelines[key]
    const ratio = total > 0 ? value / total : 0
    const length = ratio * CIRCUMFERENCE
    const offset = SEGMENTS.slice(0, index).reduce((sum, segment) => {
      const segmentValue = pipelines[segment.key]
      const segmentRatio = total > 0 ? segmentValue / total : 0
      return sum + segmentRatio * CIRCUMFERENCE
    }, 0)
    return { key, color, length, offset }
  })

  return (
    <DashboardCard title="Pipelines">
      <div className={dashboardDonutLayoutClass}>
          <svg width={SIZE} height={SIZE} viewBox={`0 0 ${SIZE} ${SIZE}`} role="img" aria-label="Pipeline status chart">
            <circle
              cx={SIZE / 2}
              cy={SIZE / 2}
              r={RADIUS}
              fill="none"
              stroke="var(--border)"
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
              fontFamily="var(--font-sans)"
            >
              {total}
            </text>
          </svg>
          <div className={dashboardLegendClass}>
            {SEGMENTS.map(({ key, label, color }) => (
              <div key={key} className={dashboardLegendRowClass}>
                <span className={dashboardDotClass} style={{ background: color }} />
                <span className={dashboardLegendLabelClass}>{label}</span>
                <span className={dashboardLegendValueClass}>{pipelines[key]}</span>
              </div>
            ))}
          </div>
      </div>
    </DashboardCard>
  )
}
