export interface ChartSeriesEntry {
  stroke: string
  dash: string
}

export const chartSeries: readonly ChartSeriesEntry[] = [
  { stroke: 'var(--chart-series-1)', dash: '0' },
  { stroke: 'var(--chart-series-2)', dash: '6 3' },
  { stroke: 'var(--chart-series-3)', dash: '2 3' },
  { stroke: 'var(--chart-series-4)', dash: '8 2 2 2' },
  { stroke: 'var(--chart-series-5)', dash: '4 2' },
  { stroke: 'var(--chart-series-6)', dash: '1 3' },
] as const

export function chartSeriesAt(
  index: number,
  series: readonly ChartSeriesEntry[] = chartSeries,
): ChartSeriesEntry {
  if (series.length === 0) {
    throw new Error('chartSeriesAt requires at least one series entry')
  }
  const normalizedIndex = ((index % series.length) + series.length) % series.length
  return series[normalizedIndex]
}

export const CHART_SERIES_MEMORY = chartSeriesAt(0)
export const CHART_SERIES_HTTP_LATENCY = chartSeriesAt(1)
export const CHART_SERIES_CPU = chartSeriesAt(2)
export const CHART_SERIES_MCP_LATENCY = chartSeriesAt(3)

export const metricChartSeries = {
  memory: CHART_SERIES_MEMORY,
  httpLatency: CHART_SERIES_HTTP_LATENCY,
  cpu: CHART_SERIES_CPU,
  mcpLatency: CHART_SERIES_MCP_LATENCY,
} as const
