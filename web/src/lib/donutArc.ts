export interface DonutSegmentInput {
  value: number
}

export interface DonutArcRender<T extends DonutSegmentInput> {
  segment: T
  pathD: string
}

export const DEFAULT_FULL_CIRCLE_EPSILON_DEG = 0.000001

function clampSweepDeg(sweep: number): number {
  return Math.min(Math.max(sweep, 0), 360)
}

function assertValidSegmentValue(value: number, index: number): void {
  if (!Number.isFinite(value) || value < 0) {
    throw new Error(`Invalid donut segment value at index ${index}: expected finite non-negative number`)
  }
}

/**
 * Render an SVG arc path from polar angles.
 *
 * The radius must be finite and positive. Coordinates may be any finite SVG
 * coordinate values supplied by the caller. Angles are in degrees, with 0deg at
 * twelve o'clock and positive sweeps moving clockwise.
 *
 * endAngleDeg must be greater than or equal to startAngleDeg. Sweeps are
 * clamped into 0..360 degrees by clampSweepDeg, and a full or epsilon-close
 * full circle is split into two arc commands because SVG cannot represent it
 * as one closed arc.
 */
export function describeArcPath(
  cx: number,
  cy: number,
  r: number,
  startAngleDeg: number,
  endAngleDeg: number,
  fullCircleEpsilonDeg = DEFAULT_FULL_CIRCLE_EPSILON_DEG,
): string {
  if (!Number.isFinite(r) || r <= 0) {
    throw new Error('describeArcPath requires a finite positive radius')
  }
  if (endAngleDeg < startAngleDeg) {
    throw new Error('describeArcPath requires endAngleDeg >= startAngleDeg')
  }

  const polar = (angleDeg: number) => {
    const rad = ((angleDeg - 90) * Math.PI) / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }
  const sweep = clampSweepDeg(endAngleDeg - startAngleDeg)
  // SVG arcs cannot represent a closed full circle as one segment.
  if (360 - sweep <= fullCircleEpsilonDeg) {
    const start = polar(startAngleDeg)
    const mid = polar(startAngleDeg + 180)
    return [
      `M ${start.x} ${start.y}`,
      `A ${r} ${r} 0 1 1 ${mid.x} ${mid.y}`,
      `A ${r} ${r} 0 1 1 ${start.x} ${start.y}`,
    ].join(' ')
  }
  const start = polar(startAngleDeg)
  const end = polar(startAngleDeg + sweep)
  const largeArcFlag = sweep > 180 ? '1' : '0'
  return `M ${start.x} ${start.y} A ${r} ${r} 0 ${largeArcFlag} 1 ${end.x} ${end.y}`
}

/**
 * Convert non-negative segment values into clockwise donut arcs around the
 * supplied center/radius. Segment values must be finite and non-negative; the
 * radius must be finite and positive. A zero total returns no arcs.
 */
export function donutArcs<T extends DonutSegmentInput>(
  segments: T[],
  cx: number,
  cy: number,
  r: number,
  fullCircleEpsilonDeg = DEFAULT_FULL_CIRCLE_EPSILON_DEG,
): DonutArcRender<T>[] {
  if (segments.length === 0) return []

  segments.forEach((segment, index) => {
    assertValidSegmentValue(segment.value, index)
  })
  const total = segments.reduce((sum, s) => sum + s.value, 0)
  if (total <= 0) return []
  let cursorDeg = 0
  return segments.map((segment) => {
    const sweep = (segment.value / total) * 360
    const startDeg = cursorDeg
    const endDeg = cursorDeg + sweep
    cursorDeg = endDeg
    return {
      segment,
      pathD: describeArcPath(cx, cy, r, startDeg, endDeg, fullCircleEpsilonDeg),
    }
  })
}
