import type { ModelBreakdown } from '../../types/tokens'

const COLORS = [
  '#3b82f6',
  '#10b981',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#06b6d4',
  '#84cc16',
  '#f97316',
]

interface Props {
  items: ModelBreakdown[]
}

export function ModelDistributionBar({ items }: Props) {
  if (items.length === 0) {
    return null
  }

  const total = items.reduce((sum, item) => sum + item.totalTokens, 0)
  if (total <= 0) {
    return null
  }
  const rawPercents = items.map((item) => (item.totalTokens / total) * 100)
  const minPercent = 2
  const clampedWidths = rawPercents.map((percent) => Math.max(percent, minPercent))
  const clampedTotal = clampedWidths.reduce((sum, width) => sum + width, 0)
  const scale = clampedTotal > 100 ? 100 / clampedTotal : 1
  const widths = clampedWidths.map((width) => width * scale)

  return (
    <div className="mt-3 flex h-3 overflow-hidden rounded-full bg-background/70">
      {items.map((item, index) => {
        const width = widths[index] ?? 0
        return (
          <div
            key={item.family}
            title={`${item.family}: ${item.totalTokens.toLocaleString()} tokens`}
            style={{
              width: `${width}%`,
              backgroundColor: COLORS[index % COLORS.length],
            }}
          />
        )
      })}
    </div>
  )
}
