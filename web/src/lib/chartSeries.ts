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

export function chartSeriesAt(index: number): ChartSeriesEntry {
  return chartSeries[index % chartSeries.length]
}
