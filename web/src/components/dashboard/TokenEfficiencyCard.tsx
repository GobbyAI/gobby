import { useMemo } from 'react'
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts'
import { useTokenTimeSeries } from '../../hooks/useTokenTimeSeries'
import { useSavings } from '../../hooks/useSavings'
import { useUsage } from '../../hooks/useUsage'
import { useModelBreakdown } from '../../hooks/useModelBreakdown'
import { cn } from '../../lib/utils'
import { DashboardCard } from './DashboardCard'
import { ModelDistributionBar } from './ModelDistributionBar'
import { ModelBreakdownList } from './ModelBreakdownList'
import {
  dashboardBreakdownClass,
  dashboardBreakdownLabelClass,
  dashboardBreakdownRowClass,
  dashboardBreakdownValueClass,
  dashboardChartEmptyClass,
  dashboardEfficiencyClass,
  dashboardFullCardClass,
} from './dashboardStyles'
import type { TimeSeriesGranularity } from '../../types/tokens'

interface ChartPoint {
  time: string
  ts: number
  tokens_spent: number
  tokens_saved: number
}

function formatTime(ts: string, granularity: TimeSeriesGranularity): string {
  const d = new Date(ts)
  if (granularity === '1d') {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
  }
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function formatTokensShort(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(0)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`
  return String(n)
}

function granularityForHours(hours: number): TimeSeriesGranularity {
  if (hours <= 6) return '30m'
  if (hours <= 168) return '1h'
  return '1d'
}

const CHART_MARGIN = { top: 5, right: 10, left: 0, bottom: 5 }
const GRID_STROKE = 'color-mix(in srgb, var(--text-primary) 6%, transparent)'
const AXIS_STYLE = { fontSize: 10, fill: 'var(--text-secondary)' }

const CATEGORY_LABELS: Record<string, string> = {
  compression: 'Compression',
  code_index: 'Code Index',
  discovery: 'Discovery',
}

interface Props {
  hours: number
  projectId?: string
}

export function TokenEfficiencyCard({ hours, projectId }: Props) {
  const tokenEventsEnabled =
    import.meta.env.VITE_TOKEN_EVENTS !== '0' &&
    import.meta.env.VITE_TOKEN_EVENTS !== 'false'
  const granularity = granularityForHours(hours)
  const { data: tsData, isLoading } = useTokenTimeSeries(hours, projectId, granularity)
  const { data: savingsData } = useSavings(hours, projectId)
  const { data: usageData } = useUsage(hours, projectId)
  const { data: modelBreakdown = [] } = useModelBreakdown(hours, projectId)

  const chartData = useMemo<ChartPoint[]>(() => {
    if (!tsData?.buckets) return []
    return tsData.buckets.map(b => ({
      time: formatTime(b.timestamp, granularity),
      ts: new Date(b.timestamp).getTime(),
      tokens_spent: b.tokens_spent,
      tokens_saved: b.tokens_saved,
    }))
  }, [granularity, tsData])

  // Compute efficiency ratio from totals
  const totalSpent = usageData
    ? usageData.totals.input_tokens + usageData.totals.output_tokens
    : 0
  const totalSaved = savingsData?.total_tokens_saved ?? 0
  const totalWork = totalSpent + totalSaved
  const efficiencyPct = totalWork > 0
    ? Math.round((totalSaved / totalWork) * 100)
    : 0

  const hasData = chartData.length > 0
  const categories = savingsData?.categories ?? {}
  const efficiencyBadge = efficiencyPct > 0 ? (
    <span className={cn('text-xs font-semibold', dashboardEfficiencyClass(efficiencyPct))}>
      {efficiencyPct}% efficiency
    </span>
  ) : undefined

  return (
    <DashboardCard
      title="Token Efficiency"
      className={dashboardFullCardClass}
      action={efficiencyBadge}
    >
        {isLoading && !hasData ? (
          <div className={dashboardChartEmptyClass}>Loading token data...</div>
        ) : hasData ? (
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={chartData} margin={CHART_MARGIN}>
              <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
              <XAxis dataKey="time" tick={AXIS_STYLE} interval="preserveStartEnd" />
              <YAxis
                tick={AXIS_STYLE}
                width={45}
                tickFormatter={formatTokensShort}
              />
              <Tooltip
                contentStyle={{
                  background: 'var(--bg-secondary)',
                  border: '1px solid var(--border)',
                  fontSize: 12,
                }}
                formatter={(value, name) => [
                  formatTokens(Number(value)),
                  String(name) === 'tokens_spent' ? 'Spent' : 'Saved',
                ]}
              />
              <Area
                type="monotone"
                dataKey="tokens_spent"
                name="tokens_spent"
                stroke="var(--color-info)"
                fill="color-mix(in srgb, var(--color-info) 20%, transparent)"
              />
              <Area
                type="monotone"
                dataKey="tokens_saved"
                name="tokens_saved"
                stroke="var(--color-success-foreground)"
                fill="color-mix(in srgb, var(--color-success-foreground) 20%, transparent)"
              />
              <Legend
                iconSize={8}
                wrapperStyle={{ fontSize: 11 }}
                formatter={(value: string) =>
                  value === 'tokens_spent' ? 'Tokens Spent' : 'Tokens Saved'
                }
              />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className={dashboardChartEmptyClass}>
            No token data yet.
          </div>
        )}

        {tokenEventsEnabled && modelBreakdown.length > 0 && (
          <>
            <ModelDistributionBar items={modelBreakdown} />
            <ModelBreakdownList items={modelBreakdown} />
          </>
        )}

        {Object.keys(categories).length > 0 && (
          <div className={dashboardBreakdownClass}>
            {Object.entries(categories)
              .filter(([, catData]) => catData.tokens_saved > 0)
              .sort(([, a], [, b]) => b.tokens_saved - a.tokens_saved)
              .map(([cat, catData]) => (
              <div key={cat} className={dashboardBreakdownRowClass}>
                <span className={dashboardBreakdownLabelClass}>
                  {CATEGORY_LABELS[cat] ?? cat}
                </span>
                <span className={cn(dashboardBreakdownValueClass, 'text-success-foreground')}>
                  {formatTokens(catData.tokens_saved)} saved
                </span>
              </div>
            ))}
          </div>
        )}
    </DashboardCard>
  )
}
