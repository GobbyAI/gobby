import { useMemo, useRef } from 'react'
import type { SpanRecord } from '../../hooks/useTraces'
import { isLLMSpan, parseLLMAttributes, formatTokenCount } from './llm-utils'
import { cn } from '../../lib/utils'

interface TraceWaterfallProps {
  spans: SpanRecord[]
  onSelectSpan: (id: string) => void
  selectedSpanId: string | null
}

const ROW_HEIGHT = 32
const ROW_GAP = 4
const HEADER_HEIGHT = 40
const LABEL_WIDTH = 250
const TIMELINE_WIDTH = 800

interface SpanRow {
  span: SpanRecord
  depth: number
  row: number
}

const BAR_FILL_BY_STATUS: Record<string, string> = {
  ok: 'fill-[var(--color-success-foreground)]',
  error: 'fill-[var(--color-error)]',
  unset: 'fill-[var(--text-muted)]',
}

const BAR_FILL_LLM = 'fill-[var(--color-warning-foreground)] hover:fill-[color-mix(in_srgb,var(--color-warning-foreground)_82%,var(--text-primary))]'

function buildRows(spans: SpanRecord[]): SpanRow[] {
  const childrenMap = new Map<string, SpanRecord[]>()
  const rootSpans: SpanRecord[] = []

  for (const span of spans) {
    if (!span.parent_id) {
      rootSpans.push(span)
    } else {
      const children = childrenMap.get(span.parent_id) || []
      children.push(span)
      childrenMap.set(span.parent_id, children)
    }
  }

  for (const children of childrenMap.values()) {
    children.sort((a, b) => a.start_time_ns - b.start_time_ns)
  }
  rootSpans.sort((a, b) => a.start_time_ns - b.start_time_ns)

  const rows: SpanRow[] = []
  let currentRow = 0

  function traverse(span: SpanRecord, depth: number) {
    rows.push({ span, depth, row: currentRow++ })
    const children = childrenMap.get(span.span_id) || []
    for (const child of children) {
      traverse(child, depth + 1)
    }
  }

  for (const root of rootSpans) {
    traverse(root, 0)
  }

  const visited = new Set(rows.map(r => r.span.span_id))
  for (const span of spans) {
    if (!visited.has(span.span_id)) {
      rows.push({ span, depth: 0, row: currentRow++ })
    }
  }

  return rows
}

function formatNsToMs(ns: number): string {
  return (ns / 1_000_000).toFixed(2) + 'ms'
}

export function TraceWaterfall({ spans, onSelectSpan, selectedSpanId }: TraceWaterfallProps) {
  const svgRef = useRef<SVGSVGElement>(null)

  const rows = useMemo(() => buildRows(spans), [spans])

  const { minTime, totalTime } = useMemo(() => {
    if (spans.length === 0) return { minTime: 0, totalTime: 1 }
    let min = spans[0].start_time_ns
    let max = spans[0].end_time_ns
    for (const s of spans) {
      if (s.start_time_ns < min) min = s.start_time_ns
      if (s.end_time_ns > max) max = s.end_time_ns
    }
    return { minTime: min, totalTime: Math.max(max - min, 1) }
  }, [spans])

  const svgWidth = LABEL_WIDTH + TIMELINE_WIDTH
  const svgHeight = Math.max(HEADER_HEIGHT + rows.length * (ROW_HEIGHT + ROW_GAP) + 20, 200)

  const timeToX = (t: number) => {
    const frac = (t - minTime) / totalTime
    return LABEL_WIDTH + frac * TIMELINE_WIDTH
  }

  const rowToY = (row: number) => HEADER_HEIGHT + row * (ROW_HEIGHT + ROW_GAP)

  const gridLines = [0, 0.25, 0.5, 0.75, 1]

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex-1 overflow-auto">
        <svg
          ref={svgRef}
          width={svgWidth}
          height={svgHeight}
          className="min-w-full"
        >
          {/* Header background */}
          <rect
            x={0} y={0} width={svgWidth} height={HEADER_HEIGHT}
            className="fill-[var(--bg-secondary)] stroke-[var(--border)] [stroke-width:1]"
          />

          {/* Grid lines */}
          {gridLines.map((frac, i) => {
            const x = LABEL_WIDTH + frac * TIMELINE_WIDTH
            const timeNs = minTime + frac * totalTime
            const relativeMs = ((timeNs - minTime) / 1_000_000).toFixed(1) + 'ms'
            return (
              <g key={i}>
                <line
                  x1={x} y1={HEADER_HEIGHT} x2={x} y2={svgHeight}
                  className="stroke-[var(--border)] [stroke-width:1] [stroke-dasharray:4_4]"
                />
                <text
                  x={x} y={HEADER_HEIGHT - 10}
                  className="fill-[var(--text-secondary)] font-mono text-[length:var(--text-2xs)]"
                  textAnchor={i === 0 ? 'start' : i === 4 ? 'end' : 'middle'}
                >
                  {relativeMs}
                </text>
              </g>
            )
          })}

          {/* Rows */}
          {rows.map(({ span, depth, row }) => {
            const y = rowToY(row)
            const isSelected = selectedSpanId === span.span_id

            const x = timeToX(span.start_time_ns)
            const w = Math.max(timeToX(span.end_time_ns) - x, 2)

            const llm = isLLMSpan(span)
            const llmAttrs = llm ? parseLLMAttributes(span.attributes_json) : null
            const fillClass = llm
              ? BAR_FILL_LLM
              : (BAR_FILL_BY_STATUS[span.status.toLowerCase()] ?? BAR_FILL_BY_STATUS.unset)
            const label = llmAttrs ? llmAttrs.model : span.name
            const tokenBadge = llmAttrs && (llmAttrs.promptTokens > 0 || llmAttrs.completionTokens > 0)
              ? `${formatTokenCount(llmAttrs.promptTokens)}→${formatTokenCount(llmAttrs.completionTokens)}`
              : null

            return (
              <g key={span.span_id}>
                {/* Row stripe */}
                {row % 2 === 0 && (
                  <rect
                    x={0} y={y} width={svgWidth} height={ROW_HEIGHT}
                    className="fill-[var(--bg-secondary)] opacity-50"
                  />
                )}

                {/* Label */}
                <text
                  x={8 + depth * 12}
                  y={y + ROW_HEIGHT / 2 + 4}
                  className="fill-[var(--text-primary)] font-mono text-[length:var(--text-xs)] select-none"
                >
                  {label.length > 25 ? label.slice(0, 25) + '...' : label}
                </text>

                {/* Bar */}
                <rect
                  x={x} y={y + 6} width={w} height={ROW_HEIGHT - 12}
                  rx={2} ry={2}
                  className={cn(
                    'cursor-pointer transition-opacity duration-200 hover:opacity-80',
                    fillClass,
                    isSelected && 'stroke-[var(--text-primary)] [stroke-width:2px]',
                  )}
                  onClick={() => onSelectSpan(span.span_id)}
                >
                  <title>{span.name} ({formatNsToMs(span.end_time_ns - span.start_time_ns)})</title>
                </rect>

                {/* Token badge */}
                {tokenBadge && (
                  <text
                    x={Math.min(x + w + 4, LABEL_WIDTH + TIMELINE_WIDTH - 60)}
                    y={y + ROW_HEIGHT / 2 + 3}
                    className="fill-[var(--text-primary)] font-mono text-[length:var(--text-2xs)] opacity-85"
                  >
                    {tokenBadge}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      </div>
    </div>
  )
}
