import { useMemo } from 'react'
import {
  ResponsiveContainer,
  LineChart,
  Line,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts'
import { useMetricSnapshots, type MetricSnapshot } from '../../hooks/useMetrics'
import { DashboardCard } from './DashboardCard'
import {
  dashboardChartCellClass,
  dashboardChartEmptyClass,
  dashboardChartGridClass,
  dashboardChartLabelClass,
  dashboardFullCardClass,
} from './dashboardStyles'

interface ChartPoint {
  time: string
  ts: number
  // HTTP traffic
  httpReqs: number
  httpErrors: number
  // MCP operations
  mcpCalls: number
  mcpErrors: number
  // System resources
  memoryMb: number
  cpuPercent: number
  // Latency
  httpLatencyMs: number
  mcpLatencyMs: number
}

function formatTime(ts: string): string {
  const d = new Date(ts + 'Z')
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function getCounter(snap: MetricSnapshot, name: string): number {
  return snap.metrics.counters?.[name]?.value ?? 0
}

function getGauge(snap: MetricSnapshot, name: string): number {
  return snap.metrics.gauges?.[name]?.value ?? 0
}

function getHistogramAvg(snap: MetricSnapshot, name: string): number {
  const h = snap.metrics.histograms?.[name]
  if (!h || !h.count) return 0
  return h.sum / h.count
}

function buildChartData(snapshots: MetricSnapshot[]): ChartPoint[] {
  if (snapshots.length === 0) return []

  const points: ChartPoint[] = []

  for (let i = 0; i < snapshots.length; i++) {
    const snap = snapshots[i]
    const prev = i > 0 ? snapshots[i - 1] : null

    // Delta computation for cumulative counters
    const httpReqs = prev
      ? Math.max(0, getCounter(snap, 'http_requests_total') - getCounter(prev, 'http_requests_total'))
      : 0
    const httpErrors = prev
      ? Math.max(0, getCounter(snap, 'http_errors_total') - getCounter(prev, 'http_errors_total'))
      : 0
    const mcpCalls = prev
      ? Math.max(0, getCounter(snap, 'mcp_tool_calls_total') - getCounter(prev, 'mcp_tool_calls_total'))
      : 0
    const mcpErrors = prev
      ? Math.max(0, getCounter(snap, 'mcp_tool_errors_total') - getCounter(prev, 'mcp_tool_errors_total'))
      : 0

    points.push({
      time: formatTime(snap.timestamp),
      ts: new Date(snap.timestamp + 'Z').getTime(),
      httpReqs,
      httpErrors,
      mcpCalls,
      mcpErrors,
      memoryMb: Math.round(getGauge(snap, 'daemon_memory_usage_bytes') / (1024 * 1024) * 10) / 10,
      cpuPercent: Math.round(getGauge(snap, 'daemon_cpu_percent') * 10) / 10,
      httpLatencyMs: Math.round(getHistogramAvg(snap, 'http_request_duration_seconds') * 1000 * 10) / 10,
      mcpLatencyMs: Math.round(getHistogramAvg(snap, 'mcp_tool_call_duration_seconds') * 1000 * 10) / 10,
    })
  }

  // Skip the first point since it has no delta
  return points.slice(1)
}

const CHART_MARGIN = { top: 5, right: 10, left: 0, bottom: 5 }
const GRID_STROKE = 'color-mix(in srgb, var(--text-primary) 6%, transparent)'
const AXIS_STYLE = { fontSize: 10, fill: 'var(--text-secondary)' }

function EmptyChart() {
  return (
    <div className={dashboardChartEmptyClass}>
      No metrics data yet. Snapshots are taken every 60s.
    </div>
  )
}

interface Props {
  hours: number
}

export function MetricsChartsCard({ hours }: Props) {
  // Metrics snapshots max out at 24h of data, clamp accordingly
  const metricsHours = hours === 0 ? 24 : Math.min(hours, 24)
  const { data: snapshots, isLoading } = useMetricSnapshots(metricsHours)
  const chartData = useMemo(() => buildChartData(snapshots), [snapshots])

  const hasData = chartData.length > 0

  return (
    <DashboardCard title="Metrics" className={dashboardFullCardClass}>
        {isLoading && !hasData ? (
          <div className="flex items-center justify-center p-5 text-sm text-muted-foreground">
            Loading metrics...
          </div>
        ) : (
          <div className={dashboardChartGridClass}>
            {/* HTTP Traffic */}
            <div className={dashboardChartCellClass}>
              <div className={dashboardChartLabelClass}>HTTP Traffic (per interval)</div>
              {hasData ? (
                <ResponsiveContainer width="100%" height={160}>
                  <AreaChart data={chartData} margin={CHART_MARGIN}>
                    <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                    <XAxis dataKey="time" tick={AXIS_STYLE} interval="preserveStartEnd" />
                    <YAxis tick={AXIS_STYLE} width={35} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', fontSize: 12 }}
                    />
                    <Area type="monotone" dataKey="httpReqs" name="Requests" stroke="var(--color-info)" fill="color-mix(in srgb, var(--color-info) 15%, transparent)" />
                    <Area type="monotone" dataKey="httpErrors" name="Errors" stroke="var(--color-error)" fill="color-mix(in srgb, var(--color-error) 15%, transparent)" />
                    <Legend iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : <EmptyChart />}
            </div>

            {/* MCP Operations */}
            <div className={dashboardChartCellClass}>
              <div className={dashboardChartLabelClass}>MCP Operations (per interval)</div>
              {hasData ? (
                <ResponsiveContainer width="100%" height={160}>
                  <AreaChart data={chartData} margin={CHART_MARGIN}>
                    <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                    <XAxis dataKey="time" tick={AXIS_STYLE} interval="preserveStartEnd" />
                    <YAxis tick={AXIS_STYLE} width={35} allowDecimals={false} />
                    <Tooltip
                      contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', fontSize: 12 }}
                    />
                    <Area type="monotone" dataKey="mcpCalls" name="Calls" stroke="var(--color-info)" fill="color-mix(in srgb, var(--color-info) 15%, transparent)" />
                    <Area type="monotone" dataKey="mcpErrors" name="Errors" stroke="var(--color-error)" fill="color-mix(in srgb, var(--color-error) 15%, transparent)" />
                    <Legend iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  </AreaChart>
                </ResponsiveContainer>
              ) : <EmptyChart />}
            </div>

            {/* System Resources */}
            <div className={dashboardChartCellClass}>
              <div className={dashboardChartLabelClass}>System Resources</div>
              {hasData ? (
                <ResponsiveContainer width="100%" height={160}>
                  <LineChart data={chartData} margin={CHART_MARGIN}>
                    <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                    <XAxis dataKey="time" tick={AXIS_STYLE} interval="preserveStartEnd" />
                    <YAxis yAxisId="mem" tick={AXIS_STYLE} width={35} />
                    <YAxis yAxisId="cpu" orientation="right" tick={AXIS_STYLE} width={35} unit="%" />
                    <Tooltip
                      contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', fontSize: 12 }}
                    />
                    <Line yAxisId="mem" type="monotone" dataKey="memoryMb" name="Memory (MB)" stroke="#22c55e" dot={false} />
                    <Line yAxisId="cpu" type="monotone" dataKey="cpuPercent" name="CPU (%)" stroke="#f59e0b" dot={false} />
                    <Legend iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : <EmptyChart />}
            </div>

            {/* Operation Latency */}
            <div className={dashboardChartCellClass}>
              <div className={dashboardChartLabelClass}>Avg Latency (ms)</div>
              {hasData ? (
                <ResponsiveContainer width="100%" height={160}>
                  <LineChart data={chartData} margin={CHART_MARGIN}>
                    <CartesianGrid strokeDasharray="3 3" stroke={GRID_STROKE} />
                    <XAxis dataKey="time" tick={AXIS_STYLE} interval="preserveStartEnd" />
                    <YAxis tick={AXIS_STYLE} width={35} />
                    <Tooltip
                      contentStyle={{ background: 'var(--bg-secondary)', border: '1px solid var(--border)', fontSize: 12 }}
                    />
                    <Line type="monotone" dataKey="httpLatencyMs" name="HTTP" stroke="#3b82f6" dot={false} />
                    <Line type="monotone" dataKey="mcpLatencyMs" name="MCP" stroke="#8b5cf6" dot={false} />
                    <Legend iconSize={8} wrapperStyle={{ fontSize: 11 }} />
                  </LineChart>
                </ResponsiveContainer>
              ) : <EmptyChart />}
            </div>
          </div>
        )}
    </DashboardCard>
  )
}
