export interface DonutSegmentInput {
  value: number
}

export interface DonutArcRender<T extends DonutSegmentInput> {
  segment: T
  pathD: string
}

const FULL_CIRCLE_THRESHOLD = 359.999

export function describeArcPath(
  cx: number,
  cy: number,
  r: number,
  startAngleDeg: number,
  endAngleDeg: number,
): string {
  const polar = (angleDeg: number) => {
    const rad = ((angleDeg - 90) * Math.PI) / 180
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) }
  }
  const sweep = endAngleDeg - startAngleDeg
  // SVG arcs cannot represent a closed full circle as one segment.
  if (sweep >= FULL_CIRCLE_THRESHOLD) {
    const start = polar(startAngleDeg)
    const mid = polar(startAngleDeg + 180)
    return [
      `M ${start.x} ${start.y}`,
      `A ${r} ${r} 0 1 1 ${mid.x} ${mid.y}`,
      `A ${r} ${r} 0 1 1 ${start.x} ${start.y}`,
    ].join(' ')
  }
  const start = polar(startAngleDeg)
  const end = polar(endAngleDeg)
  const largeArcFlag = sweep > 180 ? '1' : '0'
  return `M ${start.x} ${start.y} A ${r} ${r} 0 ${largeArcFlag} 1 ${end.x} ${end.y}`
}

export function donutArcs<T extends DonutSegmentInput>(
  segments: T[],
  cx: number,
  cy: number,
  r: number,
): DonutArcRender<T>[] {
  const total = segments.reduce((sum, s) => sum + s.value, 0)
  if (total <= 0) return []
  let cursorDeg = 0
  return segments.map((segment) => {
    const sweep = (segment.value / total) * 360
    const startDeg = cursorDeg
    const endDeg = cursorDeg + sweep
    cursorDeg = endDeg
    return { segment, pathD: describeArcPath(cx, cy, r, startDeg, endDeg) }
  })
}
