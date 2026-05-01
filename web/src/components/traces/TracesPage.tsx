import { useState, useEffect, useMemo } from 'react'
import { useTraces, useTraceDetail } from '../../hooks/useTraces'
import { TraceWaterfall } from './TraceWaterfall'
import { TraceDetail } from './TraceDetail'
import { SidebarPanel } from '../shared/SidebarPanel'
import { formatTime } from '../workflows/executionFormatters'
import { isLLMSpan, parseLLMAttributes, formatTokenCount } from './llm-utils'
import './TracesPage.css'

interface TracesPageProps {
  projectId?: string
  initialTraceId?: string | null
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
    <div className="traces-page">
      <div className="traces-toolbar">
        <h2 className="traces-toolbar-title">Traces</h2>
        <select
          className="traces-filter-select"
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

      <div className="traces-list">
        {traces.length === 0 && !isLoading && (
          <div className="traces-empty">No traces found</div>
        )}
        {isLoading && traces.length === 0 && (
          <div className="traces-empty">Loading...</div>
        )}

        {traces.map((trace) => {
          const isSelected = trace.trace_id === selectedTraceId
          const llmTokens = isSelected ? llmTokensForSelected : 0
          const hasLLMSpans = isSelected && hasLLMSpansForSelected
          return (
            <button
              key={trace.trace_id}
              type="button"
              className={`trace-item ${isSelected ? 'selected' : ''}`}
              onClick={() => {
                setSelectedTraceId(trace.trace_id)
                setSelectedSpanId(null)
              }}
            >
              <div className="trace-item-main">
                <div className={`trace-status trace-status--${trace.status.toLowerCase()}`} title={trace.status} />
                <span className="trace-name" title={trace.root_span_name || trace.trace_id}>
                  {trace.root_span_name || 'Unknown Span'}
                </span>
                {hasLLMSpans && (
                  <span className="trace-llm-badge" title={`${formatTokenCount(llmTokens)} tokens`}>
                    LLM {formatTokenCount(llmTokens)}
                  </span>
                )}
              </div>
              <div className="trace-item-meta">
                <span className="trace-id">{trace.trace_id.slice(0, 8)}...</span>
                <span className="trace-duration">{(trace.duration_ms || 0).toFixed(2)}ms</span>
              </div>
              <div className="trace-item-meta trace-item-meta--time">
                <span className="trace-time">{formatTime(trace.timestamp)}</span>
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
        <div className="traces-content">
          {isDetailLoading && spans.length === 0 ? (
            <div className="traces-empty">Loading trace details...</div>
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
