import { useSavings } from '../../hooks/useSavings'
import { useUsage } from '../../hooks/useUsage'
import { cn } from '../../lib/utils'
import { DashboardCard } from './DashboardCard'
import {
  dashboardBigStatClass,
  dashboardBreakdownClass,
  dashboardBreakdownLabelClass,
  dashboardBreakdownRowClass,
  dashboardBreakdownValueClass,
  dashboardEfficiencyClass,
  dashboardMetaTextClass,
  dashboardSingleStatGridClass,
  dashboardStatClass,
  dashboardStatLabelClass,
  dashboardStatValueClass,
} from './dashboardStyles'

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
    <DashboardCard title="Savings">
      <div className={cn(dashboardBigStatClass, 'text-success-foreground')}>
        {formatTokens(tokensSaved)}
      </div>
      <div className={dashboardMetaTextClass}>
          Tokens Saved
          {efficiencyPct > 0 && (
            <span className={cn('ml-2', dashboardEfficiencyClass(efficiencyPct))}>
              {efficiencyPct}% efficiency
            </span>
          )}
      </div>
      <div className={dashboardSingleStatGridClass}>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{data?.total_events ?? 0}</span>
          <span className={dashboardStatLabelClass}>Savings Events</span>
        </div>
      </div>
      {Object.keys(categories).length > 0 && (
        <div className={dashboardBreakdownClass}>
          {Object.entries(categories)
            .filter(([, catData]) => catData.tokens_saved > 0)
            .map(([cat, catData]) => (
            <div key={cat} className={dashboardBreakdownRowClass}>
              <span className={dashboardBreakdownLabelClass}>
                {CATEGORY_LABELS[cat] ?? cat}
              </span>
              <span className={dashboardBreakdownValueClass}>
                {formatTokens(catData.tokens_saved)} tokens
              </span>
            </div>
          ))}
        </div>
      )}
    </DashboardCard>
  )
}
