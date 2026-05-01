import { useState, useEffect, useMemo } from 'react'
import { useTraces, useTraceDetail } from '../../hooks/useTraces'
import { TraceWaterfall } from './TraceWaterfall'
import { TraceDetail } from './TraceDetail'
import { SidebarPanel } from '../shared/SidebarPanel'
import { formatTime } from '../workflows/executionFormatters'
import { isLLMSpan, parseLLMAttributes, formatTokenCount } from './llm-utils'
import { cn } from '../../lib/utils'

interface TracesPageProps {
  projectId?: string
  initialTraceId?: string | null
}

const STATUS_DOT_BG: Record<string, string> = {
  ok: 'bg-[var(--color-success-foreground)]',
  error: 'bg-[var(--color-error)]',
  unset: 'bg-[var(--text-muted)]',
}

export function TracesPage({ projectId, initialTraceId }: TracesPageProps) {
  const { traces, isLoading, filters, setFilters, selectedTraceId, setSelectedTraceId } = useTraces(projectId)
  const { spans, isLoading: isDetailLoading } = useTraceDetail(selectedTraceId)

  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null)

  const llmTokensForSelected = useMemo(() => {
    if (!selectedTraceId) return 0
    return spans.filter(isLLMSpan).reduce((sum, s) => {
      const a = parseLLMAttributes(s.attributes_json)
      return sum + (a ? a.promptTokens + a.completionTokens : 0)
    }, 0)
  }, [selectedTraceId, spans])
  const hasLLMSpansForSelected = useMemo(
    () => !!selectedTraceId && spans.some(isLLMSpan),
    [selectedTraceId, spans],
  )

  useEffect(() => {
    if (initialTraceId && !selectedTraceId) {
      setSelectedTraceId(initialTraceId)
    }
  }, [initialTraceId, selectedTraceId, setSelectedTraceId])

  const closeTraceDrawer = () => {
    setSelectedTraceId(null)
    setSelectedSpanId(null)
  }

  return (
    <div className="flex h-full w-full flex-col overflow-hidden bg-[var(--bg-primary)] text-[var(--text-primary)]">
      <div className="flex flex-wrap items-center gap-3 border-b border-[var(--border)] px-4 py-3">
        <h2 className="m-0 min-w-0 flex-1 text-[length:var(--text-lg)] font-semibold">Traces</h2>
        <select
          className="min-h-9 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] pointer-coarse:min-h-11"
          value={filters.status || ''}
          onChange={(e) => setFilters({ ...filters, status: e.target.value || undefined })}
          aria-label="Filter by status"
        >
          <option value="">All Statuses</option>
          <option value="OK">OK</option>
          <option value="ERROR">ERROR</option>
          <option value="UNSET">UNSET</option>
        </select>
      </div>

      <div className="flex flex-1 flex-col gap-1 overflow-y-auto p-2">
        {traces.length === 0 && !isLoading && (
          <div className="px-4 py-6 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]">No traces found</div>
        )}
        {isLoading && traces.length === 0 && (
          <div className="px-4 py-6 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]">Loading...</div>
        )}

        {traces.map((trace) => {
          const isSelected = trace.trace_id === selectedTraceId
          const llmTokens = isSelected ? llmTokensForSelected : 0
          const hasLLMSpans = isSelected && hasLLMSpansForSelected
          const dotClass = STATUS_DOT_BG[trace.status.toLowerCase()] ?? STATUS_DOT_BG.unset
          return (
            <button
              key={trace.trace_id}
              type="button"
              className={cn(
                'flex w-full cursor-pointer flex-col gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2.5 text-left font-[inherit] text-[inherit] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]',
                isSelected && 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-info)_10%,transparent)]',
              )}
              onClick={() => {
                setSelectedTraceId(trace.trace_id)
                setSelectedSpanId(null)
              }}
            >
              <div className="flex min-w-0 items-center gap-2">
                <div className={cn('h-2 w-2 shrink-0 rounded-full', dotClass)} title={trace.status} />
                <span
                  className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-md)] font-medium"
                  title={trace.root_span_name || trace.trace_id}
                >
                  {trace.root_span_name || 'Unknown Span'}
                </span>
                {hasLLMSpans && (
                  <span
                    className="shrink-0 rounded-sm bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] px-1 py-px text-[length:var(--text-2xs)] font-semibold tracking-[0.03em] text-[var(--color-warning-foreground)]"
                    title={`${formatTokenCount(llmTokens)} tokens`}
                  >
                    LLM {formatTokenCount(llmTokens)}
                  </span>
                )}
              </div>
              <div className="flex justify-between gap-2 text-[length:var(--text-sm)] text-[var(--text-secondary)]">
                <span>{trace.trace_id.slice(0, 8)}...</span>
                <span>{(trace.duration_ms || 0).toFixed(2)}ms</span>
              </div>
              <div className="flex justify-between gap-2 text-[length:var(--text-xs)] text-[var(--text-secondary)]">
                <span>{formatTime(trace.timestamp)}</span>
              </div>
            </button>
          )
        })}
      </div>

      <SidebarPanel
        isOpen={!!selectedTraceId}
        onClose={closeTraceDrawer}
        title={selectedTraceId ? `Trace ${selectedTraceId.slice(0, 8)}` : 'Trace'}
        width={640}
      >
        <div className="flex h-full flex-col overflow-hidden">
          {isDetailLoading && spans.length === 0 ? (
            <div className="px-4 py-6 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]">Loading trace details...</div>
          ) : (
            <TraceWaterfall spans={spans} onSelectSpan={setSelectedSpanId} selectedSpanId={selectedSpanId} />
          )}
        </div>
      </SidebarPanel>

      <TraceDetail
        isOpen={!!selectedSpanId}
        onClose={() => setSelectedSpanId(null)}
        span={spans.find((s) => s.span_id === selectedSpanId)}
      />
    </div>
  )
}
