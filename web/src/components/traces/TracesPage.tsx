import { useState, useEffect, useMemo } from 'react'
import { useTraces, useTraceDetail } from '../../hooks/useTraces'
import { TraceWaterfall } from './TraceWaterfall'
import { TraceDetail } from './TraceDetail'
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

  const [sidebarOpen, setSidebarOpen] = useState(true)
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

  return (
    <div className="traces-page">
      {/* Left panel */}
      <div className={`traces-browser ${sidebarOpen ? '' : 'collapsed'}`}>
        <div className="traces-sidebar-header">
          {sidebarOpen && <span className="traces-sidebar-title">Traces</span>}
          <div className="traces-sidebar-actions">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              title={sidebarOpen ? 'Collapse' : 'Expand'}
            >
              {sidebarOpen ? '\u25C0' : '\u25B6'}
            </button>
          </div>
        </div>

        {sidebarOpen && (
          <>
            <div className="traces-filter-bar">
              <select
                className="traces-filter-select"
                value={filters.status || ''}
                onChange={(e) => setFilters({ ...filters, status: e.target.value || undefined })}
              >
                <option value="">All Statuses</option>
                <option value="OK">OK</option>
                <option value="ERROR">ERROR</option>
                <option value="UNSET">UNSET</option>
              </select>
            </div>

            <div className="traces-list">
              {traces.length === 0 && !isLoading && (
                <div className="traces-empty-sidebar">No traces found</div>
              )}
              {isLoading && traces.length === 0 && (
                <div className="traces-empty-sidebar">Loading...</div>
              )}

              {traces.map((trace) => {
                const isSelected = trace.trace_id === selectedTraceId
                const llmTokens = isSelected ? llmTokensForSelected : 0
                const hasLLMSpans = isSelected && hasLLMSpansForSelected
                return (
                  <div
                    key={trace.trace_id}
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
                    <div className="trace-item-meta" style={{ marginTop: 4 }}>
                      <span className="trace-time">{formatTime(trace.timestamp)}</span>
                    </div>
                  </div>
                )
              })}
            </div>
          </>
        )}
      </div>

      {/* Main panel */}
      <div className="traces-main">
        {!selectedTraceId ? (
          <div className="traces-empty">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
            </svg>
            <h3>Select a trace</h3>
            <p>Choose a trace from the list to view its waterfall and details.</p>
          </div>
        ) : isDetailLoading && spans.length === 0 ? (
          <div className="traces-empty">Loading trace details...</div>
        ) : (
          <div className="traces-content">
            <TraceWaterfall spans={spans} onSelectSpan={setSelectedSpanId} selectedSpanId={selectedSpanId} />
          </div>
        )}

        <TraceDetail
          isOpen={!!selectedSpanId}
          onClose={() => setSelectedSpanId(null)}
          span={spans.find((s) => s.span_id === selectedSpanId)}
        />
      </div>
    </div>
  )
}
