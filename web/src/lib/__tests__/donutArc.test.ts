import { describe, expect, it } from 'vitest'
import { describeArcPath, donutArcs } from '../donutArc'

function arcCommandCount(pathD: string): number {
  return pathD.match(/\bA\b/g)?.length ?? 0
}

describe('describeArcPath', () => {
  it('renders an epsilon-close full circle as two SVG arc commands', () => {
    const pathD = describeArcPath(0, 0, 10, 0, 359.9999999)

    expect(arcCommandCount(pathD)).toBe(2)
  })

  it('clamps negative sweeps to a zero-length arc', () => {
    const pathD = describeArcPath(0, 0, 10, 0, -90)

    expect(pathD).toBe('M 6.123233995736766e-16 -10 A 10 10 0 0 1 6.123233995736766e-16 -10')
  })
})

describe('donutArcs', () => {
  it('renders a single segment as a full circle', () => {
    const [arc] = donutArcs([{ value: 5 }], 0, 0, 10)

    expect(arcCommandCount(arc.pathD)).toBe(2)
  })

  it.each([
    { value: Number.NaN, label: 'NaN' },
    { value: Number.POSITIVE_INFINITY, label: 'Infinity' },
    { value: -1, label: 'negative' },
  ])('rejects $label segment values', ({ value }) => {
    expect(() => donutArcs([{ value }], 0, 0, 10)).toThrow(
      'Invalid donut segment value at index 0: expected finite non-negative number',
    )
  })
})
