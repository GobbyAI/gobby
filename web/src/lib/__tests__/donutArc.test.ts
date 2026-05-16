import { describe, expect, it } from 'vitest'
import { DEFAULT_FULL_CIRCLE_EPSILON_DEG, describeArcPath, donutArcs } from '../donutArc'

function arcCommandCount(pathD: string): number {
  return pathD.match(/\bA\b/g)?.length ?? 0
}

describe('describeArcPath', () => {
  it('exports a named full-circle epsilon', () => {
    expect(DEFAULT_FULL_CIRCLE_EPSILON_DEG).toBeGreaterThan(0)
  })

  it('renders an epsilon-close full circle as two SVG arc commands', () => {
    const pathD = describeArcPath(0, 0, 10, 0, 359.9999999)

    expect(arcCommandCount(pathD)).toBe(2)
  })

  it('uses an explicit looser full-circle epsilon', () => {
    const pathD = describeArcPath(0, 0, 10, 0, 359, 2)

    expect(arcCommandCount(pathD)).toBe(2)
  })

  it('rejects reversed angle ordering', () => {
    expect(() => describeArcPath(0, 0, 10, 0, -90)).toThrow(
      'describeArcPath requires endAngleDeg >= startAngleDeg',
    )
  })

  it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])(
    'rejects invalid radius %s',
    (radius) => {
      expect(() => describeArcPath(0, 0, radius, 0, 90)).toThrow(
        'describeArcPath requires a finite positive radius',
      )
    },
  )
})

describe('donutArcs', () => {
  it('returns no arcs for empty input', () => {
    expect(donutArcs([], 0, 0, 10)).toEqual([])
  })

  it('renders a single segment as a full circle', () => {
    const [arc] = donutArcs([{ value: 5 }], 0, 0, 10)

    expect(arcCommandCount(arc.pathD)).toBe(2)
  })

  it('passes an explicit full-circle epsilon through to each arc', () => {
    const [arc] = donutArcs([{ value: 359 }, { value: 1 }], 0, 0, 10, 2)

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
