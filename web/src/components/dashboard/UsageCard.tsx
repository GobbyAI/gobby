import { useUsage } from '../../hooks/useUsage'
import { SOURCE_LABELS } from '../shared/sourceTheme'
import { DashboardCard } from './DashboardCard'
import {
  dashboardBreakdownClass,
  dashboardBreakdownLabelClass,
  dashboardBreakdownMonoLabelClass,
  dashboardBreakdownRowClass,
  dashboardBreakdownValueClass,
  dashboardStatClass,
  dashboardStatGridClass,
  dashboardStatLabelClass,
  dashboardStatValueClass,
} from './dashboardStyles'

function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000_000) return `${(tokens / 1_000_000_000).toFixed(1)}B`
  if (tokens >= 1_000_000) return `${(tokens / 1_000_000).toFixed(1)}M`
  if (tokens >= 1_000) return `${(tokens / 1_000).toFixed(1)}K`
  return String(tokens)
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
    session_count: 0,
  }

  const bySource = data?.by_source ?? {}
  const byModel = data?.by_model ?? {}

  const totalTokens = totals.input_tokens + totals.output_tokens

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
    <DashboardCard title="Usage">
      <div className={dashboardStatGridClass}>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{formatTokens(totalTokens)}</span>
          <span className={dashboardStatLabelClass}>Total Tokens</span>
        </div>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{totals.session_count}</span>
          <span className={dashboardStatLabelClass}>Sessions</span>
        </div>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{formatTokens(totals.input_tokens)}</span>
          <span className={dashboardStatLabelClass}>Input Tokens</span>
        </div>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{formatTokens(totals.output_tokens)}</span>
          <span className={dashboardStatLabelClass}>Output Tokens</span>
        </div>
      </div>

      {sourceEntries.length > 0 && (
        <div className={dashboardBreakdownClass}>
          {sourceEntries.map(([src, usage]) => (
            <div key={src} className={dashboardBreakdownRowClass}>
              <span className={dashboardBreakdownLabelClass}>
                {SOURCE_LABELS[src] ?? src}
              </span>
              <span className={dashboardBreakdownValueClass}>
                {formatTokens(usage.input_tokens + usage.output_tokens)}
              </span>
            </div>
          ))}
        </div>
      )}

      {topModels.length > 0 && (
        <div className={dashboardBreakdownClass}>
          {topModels.map(([model, usage]) => (
            <div key={model} className={dashboardBreakdownRowClass}>
              <span className={dashboardBreakdownMonoLabelClass}>
                {model.length > 28 ? model.slice(0, 28) + '...' : model}
              </span>
              <span className={dashboardBreakdownValueClass}>
                {formatTokens(usage.input_tokens + usage.output_tokens)}
              </span>
            </div>
          ))}
        </div>
      )}
    </DashboardCard>
  )
}
