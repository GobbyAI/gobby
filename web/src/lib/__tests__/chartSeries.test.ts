import { describe, expect, it } from 'vitest'
import {
  CHART_SERIES_MCP_LATENCY,
  CHART_SERIES_PAIRED_LATENCY,
  chartSeries,
  chartSeriesAt,
} from '../chartSeries'

describe('chartSeriesAt', () => {
  it('returns positive indexes directly', () => {
    expect(chartSeriesAt(0)).toEqual(chartSeries[0])
    expect(chartSeriesAt(1)).toEqual(chartSeries[1])
    expect(chartSeriesAt(3)).toEqual(chartSeries[3])
  })

  it('uses the fifth series for MCP latency', () => {
    expect(CHART_SERIES_MCP_LATENCY).toEqual(chartSeries[4])
  })

  it('uses the reserved fourth series for paired latency', () => {
    expect(CHART_SERIES_PAIRED_LATENCY).toEqual(chartSeries[3])
  })

  it('wraps indexes at the series boundary', () => {
    expect(chartSeriesAt(chartSeries.length)).toEqual(chartSeries[0])
  })

  it('wraps larger positive indexes with modulo arithmetic', () => {
    expect(chartSeriesAt(chartSeries.length + 2)).toEqual(chartSeries[2])
  })

  it('wraps single-entry series for any index', () => {
    const single = [chartSeriesAt(1)]
    expect(chartSeriesAt(0, single)).toEqual(chartSeries[1])
    expect(chartSeriesAt(1, single)).toEqual(chartSeries[1])
    expect(chartSeriesAt(-1, single)).toEqual(chartSeries[1])
  })

  it('wraps negative indexes back into the series', () => {
    expect(chartSeriesAt(-1)).toEqual(chartSeries[chartSeries.length - 1])
    expect(chartSeriesAt(-chartSeries.length)).toEqual(chartSeries[0])
    expect(chartSeriesAt(-chartSeries.length - 2)).toEqual(
      chartSeries[chartSeries.length - 2],
    )
  })

  it('throws when called with an empty series', () => {
    expect(() => chartSeriesAt(0, [])).toThrow('at least one series entry')
  })
})
