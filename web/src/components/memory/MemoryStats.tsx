import type { MemoryStats as MemoryStatsType } from '../../hooks/useMemory'

interface MemoryStatsProps {
  stats: MemoryStatsType | null
  isLoading: boolean
}

const TYPE_COLORS: Record<string, string> = {
  fact: 'var(--accent)',
  preference: 'var(--color-agent)',
  pattern: 'var(--color-review)',
  context: 'var(--color-warning-foreground)',
}

export function MemoryStats({ stats, isLoading }: MemoryStatsProps) {
  if (isLoading || !stats) {
    return (
      <div className="memory-stats-panel">
        <div className="memory-stats-loading">Loading stats...</div>
      </div>
    )
  }

  if (stats.total_count === 0) {
    return (
      <div className="memory-stats-panel">
        <div className="memory-stats-empty">No memories yet</div>
      </div>
    )
  }

  const importancePct = Math.round(stats.avg_importance * 100)
  const typeEntries = Object.entries(stats.by_type).sort((a, b) => b[1] - a[1])

  return (
    <div className="memory-stats-panel">
      <div className="memory-stats-total">
        <span className="memory-stats-total-count">{stats.total_count}</span>
        <span className="memory-stats-total-label">memories</span>
      </div>

      <div className="memory-stats-importance">
        <div className="memory-stats-importance-label">
          Avg Importance: {importancePct}%
        </div>
        <div className="memory-stats-importance-track">
          <div
            className="memory-stats-importance-fill"
            style={{ width: `${importancePct}%` }}
          />
        </div>
      </div>

      <div className="memory-stats-types">
        {typeEntries.map(([type, count]) => (
          <div key={type} className="memory-stats-type-row">
            <span
              className="memory-stats-type-dot"
              style={{ backgroundColor: TYPE_COLORS[type] ?? 'var(--text-muted)' }}
            />
            <span className="memory-stats-type-name">
              {type.charAt(0).toUpperCase() + type.slice(1)}
            </span>
            <span className="memory-stats-type-count">{count}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
