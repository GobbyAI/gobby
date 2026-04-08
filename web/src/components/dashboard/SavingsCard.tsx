import { useSavings } from '../../hooks/useSavings'
import { useUsage } from '../../hooks/useUsage'

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return String(tokens)
}

const CATEGORY_LABELS: Record<string, string> = {
  compression: 'Compression',
  code_index: 'Code Index',
  discovery: 'Discovery',
}

interface Props {
  hours: number
  projectId?: string
}

export function SavingsCard({ hours, projectId }: Props) {
  const { data } = useSavings(hours, projectId)
  const { data: usageData } = useUsage(hours, projectId)

  const tokensSaved = data?.total_tokens_saved ?? 0
  const tokensSpent = usageData
    ? usageData.totals.input_tokens + usageData.totals.output_tokens
    : 0
  const totalWork = tokensSpent + tokensSaved
  const efficiencyPct = totalWork > 0
    ? Math.round((tokensSaved / totalWork) * 100)
    : 0

  const categories = data?.categories ?? {}

  return (
    <div className="dash-card">
      <div className="dash-card-header">
        <h3 className="dash-card-title">Savings</h3>
      </div>
      <div className="dash-card-body">
        <div className="dash-big-stat" style={{ color: '#22c55e' }}>
          {formatTokens(tokensSaved)}
        </div>
        <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 12 }}>
          Tokens Saved
          {efficiencyPct > 0 && (
            <span style={{
              marginLeft: 8,
              color: efficiencyPct > 20 ? '#22c55e' : efficiencyPct > 10 ? '#f59e0b' : 'var(--text-secondary)',
            }}>
              {efficiencyPct}% efficiency
            </span>
          )}
        </div>
        <div className="dash-stat-grid" style={{ gridTemplateColumns: '1fr' }}>
          <div className="dash-stat">
            <span className="dash-stat-value">{data?.total_events ?? 0}</span>
            <span className="dash-stat-label">Savings Events</span>
          </div>
        </div>
        {Object.keys(categories).length > 0 && (
          <div className="dash-breakdown">
            {Object.entries(categories)
              .filter(([, catData]) => catData.tokens_saved > 0)
              .map(([cat, catData]) => (
              <div key={cat} className="dash-breakdown-row">
                <span className="dash-breakdown-label">
                  {CATEGORY_LABELS[cat] ?? cat}
                </span>
                <span className="dash-breakdown-value">
                  {formatTokens(catData.tokens_saved)} tokens
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
