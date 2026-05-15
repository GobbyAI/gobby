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
    const arc = pathD.match(
      /^M ([\d.eE+-]+) ([\d.eE+-]+) A ([\d.eE+-]+) ([\d.eE+-]+) 0 ([01]) ([01]) ([\d.eE+-]+) ([\d.eE+-]+)$/,
    )

    if (!arc) throw new Error(`Unexpected arc path: ${pathD}`)
    const [, moveX, moveY, radiusX, radiusY, largeArcFlag, sweepFlag, endX, endY] = arc

    expect(Number(radiusX)).toBeCloseTo(10)
    expect(Number(radiusY)).toBeCloseTo(10)
    expect(largeArcFlag).toBe('0')
    expect(sweepFlag).toBe('1')
    expect(Number(moveX)).toBeCloseTo(0)
    expect(Number(moveY)).toBeCloseTo(-10)
    expect(Number(endX)).toBeCloseTo(Number(moveX))
    expect(Number(endY)).toBeCloseTo(Number(moveY))
  })
})

describe('donutArcs', () => {
  it('returns no arcs for empty input', () => {
    expect(donutArcs([], 0, 0, 10)).toEqual([])
  })

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
