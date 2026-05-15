import { describe, expect, it } from 'vitest'
import { chartSeries, chartSeriesAt } from '../chartSeries'

describe('chartSeriesAt', () => {
  it('wraps negative indexes back into the series', () => {
    expect(chartSeriesAt(-1)).toEqual(chartSeries[chartSeries.length - 1])
    expect(chartSeriesAt(-chartSeries.length)).toEqual(chartSeries[0])
  })

  it('throws when called with an empty series', () => {
    expect(() => chartSeriesAt(0, [])).toThrow('at least one series entry')
  })
})
