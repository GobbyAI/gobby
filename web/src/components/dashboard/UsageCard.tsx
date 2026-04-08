import { useUsage } from '../../hooks/useUsage'
import { SOURCE_LABELS } from '../shared/sourceTheme'

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000_000) return `${(tokens / 1_000_000_000).toFixed(1)}B`
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return String(tokens)
}

function formatUsd(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`
  if (value >= 0.01) return `$${value.toFixed(3)}`
  if (value > 0) return `$${value.toFixed(4)}`
  return '$0.00'
}

interface Props {
  hours: number
  projectId?: string
}

export function UsageCard({ hours, projectId }: Props) {
  const { data } = useUsage(hours, projectId)

  const totals = data?.totals ?? {
    input_tokens: 0, output_tokens: 0,
    cache_read_tokens: 0, cache_creation_tokens: 0,
    cost_usd: 0, session_count: 0,
  }

  const bySource = data?.by_source ?? {}
  const byModel = data?.by_model ?? {}

  // Filter out sources with no usage
  const sourceEntries = Object.entries(bySource)
    .filter(([, u]) => u.input_tokens + u.output_tokens > 0)
    .sort(([, a], [, b]) => (b.input_tokens + b.output_tokens) - (a.input_tokens + a.output_tokens))

  // Filter out unknown/synthetic models and zero-usage models
  const topModels = Object.entries(byModel)
    .filter(([model, u]) => model !== 'unknown' && model !== '<synthetic>' && (u.input_tokens + u.output_tokens) > 0)
    .sort(([, a], [, b]) => (b.input_tokens + b.output_tokens) - (a.input_tokens + a.output_tokens))
    .slice(0, 5)

  return (
    <div className="dash-card">
      <div className="dash-card-header">
        <h3 className="dash-card-title">Usage</h3>
      </div>
      <div className="dash-card-body">
        <div className="dash-stat-grid">
          <div className="dash-stat">
            <span className="dash-stat-value">{formatTokens(totals.input_tokens)}</span>
            <span className="dash-stat-label">Input Tokens</span>
          </div>
          <div className="dash-stat">
            <span className="dash-stat-value">{formatTokens(totals.output_tokens)}</span>
            <span className="dash-stat-label">Output Tokens</span>
          </div>
          <div className="dash-stat">
            <span className="dash-stat-value">{formatUsd(totals.cost_usd)}</span>
            <span className="dash-stat-label">Total Cost</span>
          </div>
          <div className="dash-stat">
            <span className="dash-stat-value">{totals.session_count}</span>
            <span className="dash-stat-label">Sessions</span>
          </div>
        </div>

        {sourceEntries.length > 0 && (
          <div className="dash-breakdown">
            {sourceEntries.map(([src, usage]) => (
              <div key={src} className="dash-breakdown-row">
                <span className="dash-breakdown-label">
                  {SOURCE_LABELS[src] ?? src}
                </span>
                <span className="dash-breakdown-value">
                  {formatTokens(usage.input_tokens + usage.output_tokens)} &middot; {formatUsd(usage.cost_usd)}
                </span>
              </div>
            ))}
          </div>
        )}

        {topModels.length > 0 && (
          <div className="dash-breakdown">
            {topModels.map(([model, usage]) => (
              <div key={model} className="dash-breakdown-row">
                <span className="dash-breakdown-label dash-breakdown-label--mono">
                  {model.length > 28 ? model.slice(0, 28) + '...' : model}
                </span>
                <span className="dash-breakdown-value">
                  {formatTokens(usage.input_tokens + usage.output_tokens)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
