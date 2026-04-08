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

interface ChartPoint {
  time: string
  ts: number
  tokens_spent: number
  tokens_saved: number
}

function formatTime(ts: string): string {
  const d = new Date(ts)
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

const CHART_MARGIN = { top: 5, right: 10, left: 0, bottom: 5 }
const GRID_STROKE = 'rgba(255,255,255,0.06)'
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
  const { data: tsData, isLoading } = useTokenTimeSeries(hours, projectId)
  const { data: savingsData } = useSavings(hours, projectId)
  const { data: usageData } = useUsage(hours, projectId)

  const chartData = useMemo<ChartPoint[]>(() => {
    if (!tsData?.buckets) return []
    return tsData.buckets.map(b => ({
      time: formatTime(b.timestamp),
      ts: new Date(b.timestamp).getTime(),
      tokens_spent: b.tokens_spent,
      tokens_saved: b.tokens_saved,
    }))
  }, [tsData])

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

  const efficiencyColor =
    efficiencyPct > 20 ? '#22c55e' :
    efficiencyPct > 10 ? '#f59e0b' :
    'var(--text-secondary)'

  return (
    <div className="dash-card dash-card--full">
      <div className="dash-card-header">
        <h3 className="dash-card-title">Token Efficiency</h3>
        {efficiencyPct > 0 && (
          <span style={{
            fontSize: 12,
            fontWeight: 600,
            color: efficiencyColor,
          }}>
            {efficiencyPct}% efficiency
          </span>
        )}
      </div>
      <div className="dash-card-body">
        {isLoading && !hasData ? (
          <div className="dash-chart-empty">Loading token data...</div>
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
                  border: '1px solid var(--border-color)',
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
                stackId="tokens"
                stroke="#3b82f6"
                fill="rgba(59,130,246,0.2)"
              />
              <Area
                type="monotone"
                dataKey="tokens_saved"
                name="tokens_saved"
                stackId="tokens"
                stroke="#22c55e"
                fill="rgba(34,197,94,0.2)"
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
          <div className="dash-chart-empty">
            No token data yet.
          </div>
        )}

        {Object.keys(categories).length > 0 && (
          <div className="dash-breakdown">
            {Object.entries(categories)
              .filter(([, catData]) => catData.tokens_saved > 0)
              .sort(([, a], [, b]) => b.tokens_saved - a.tokens_saved)
              .map(([cat, catData]) => (
              <div key={cat} className="dash-breakdown-row">
                <span className="dash-breakdown-label">
                  {CATEGORY_LABELS[cat] ?? cat}
                </span>
                <span className="dash-breakdown-value" style={{ color: '#22c55e' }}>
                  {formatTokens(catData.tokens_saved)} saved
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}
