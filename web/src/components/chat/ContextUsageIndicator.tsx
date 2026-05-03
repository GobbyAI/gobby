interface ContextUsageIndicatorProps {
  totalInputTokens: number
  outputTokens: number
  contextWindow: number | null
  staleMs?: number | null
  // Cache breakdown for tooltip
  uncachedInputTokens?: number
  cacheReadTokens?: number
  cacheCreationTokens?: number
}

const STALE_THRESHOLD_MS = 60_000

export function ContextUsageIndicator({
  totalInputTokens,
  outputTokens,
  contextWindow,
  staleMs = null,
  uncachedInputTokens = 0,
  cacheReadTokens = 0,
  cacheCreationTokens = 0,
}: ContextUsageIndicatorProps) {
  const isStale = Boolean(staleMs && staleMs >= STALE_THRESHOLD_MS)

  // Context window is an INPUT limit — output tokens don't occupy it.
  // Only input tokens (uncached + cache_read + cache_creation) count toward context load.
  const percentage = contextWindow ? Math.min((totalInputTokens / contextWindow) * 100, 100) : 0
  const displayPercent = Math.round(percentage)
  const indicatorLabel = `${displayPercent}%`

  // SVG pie/ring chart
  const size = 20
  const strokeWidth = 3
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius
  const dashOffset = circumference - (percentage / 100) * circumference

  // Color based on usage: success < 50%, warning 50-80%, error > 80%
  const color = percentage > 80
    ? 'var(--color-error)'
    : percentage > 50
      ? 'var(--color-warning-foreground)'
      : 'var(--color-success-foreground)'

  const formatTokens = (n: number) => {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
    return String(n)
  }

  // Build tooltip with cache breakdown
  const tooltipLines: string[] = []
  if (contextWindow) {
    tooltipLines.push(`Context: ${formatTokens(totalInputTokens)} / ${formatTokens(contextWindow)} tokens (${displayPercent}%)`)
    tooltipLines.push('')
    tooltipLines.push(`Input: ${formatTokens(totalInputTokens)}`)
    if (cacheReadTokens > 0 || cacheCreationTokens > 0 || uncachedInputTokens > 0) {
      tooltipLines.push(`  Cache read: ${formatTokens(cacheReadTokens)}`)
      tooltipLines.push(`  Cache write: ${formatTokens(cacheCreationTokens)}`)
      tooltipLines.push(`  Uncached: ${formatTokens(uncachedInputTokens)}`)
    }
    tooltipLines.push(`Output: ${formatTokens(outputTokens)}`)
  } else {
    tooltipLines.push('Context usage: waiting for first response...')
  }
  if (isStale) {
    tooltipLines.push('')
    tooltipLines.push('Usage may be stale: no live update in the last minute.')
  }

  return (
    <div
      className="flex items-center gap-1.5 text-xs text-muted-foreground"
      style={{ opacity: isStale ? 0.55 : 1 }}
      title={tooltipLines.join('\n')}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0" style={{ transform: 'rotate(-90deg)' }}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="currentColor"
          strokeWidth={strokeWidth}
          opacity={0.15}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          strokeLinecap="round"
        />
      </svg>
      <span className="tabular-nums">{indicatorLabel}</span>
    </div>
  )
}
