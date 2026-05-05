import { memo, useMemo, useState } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { SegmentedControl } from '../ui/SegmentedControl'
import { formatTime } from '../workflows/executionFormatters'
import { useTraces, useTraceDetail } from '../../hooks/useTraces'
import type { TraceRecord, SpanRecord } from '../../hooks/useTraces'
import { ActivityPanelEmpty, TracesEmptyIcon } from './ActivityPanelEmpty'
import { ActivityRowStatusDot } from './ActivityRowStatusDot'

interface TracesTabProps {
  projectId?: string | null
}

const FILTER_OPTIONS = [
  { id: 'all', label: 'All' },
  { id: 'OK', label: 'OK' },
  { id: 'ERROR', label: 'Error' },
] as const

type StatusFilter = (typeof FILTER_OPTIONS)[number]['id']

const PAGE_SIZE = 20

export const TracesTab = memo(function TracesTab({ projectId }: TracesTabProps) {
  const { traces, isLoading, selectedTraceId, setSelectedTraceId } = useTraces(
    projectId ?? undefined,
  )
  const { spans, isLoading: isDetailLoading } = useTraceDetail(selectedTraceId)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [topHeight, setTopHeight] = useState(50)
  const [displayLimitState, setDisplayLimitState] = useState<{
    filter: StatusFilter
    limit: number
  }>({ filter: 'all', limit: PAGE_SIZE })

  const filteredTraces = useMemo(() => {
    const sorted = [...traces].sort(
      (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
    )
    if (statusFilter === 'all') return sorted
    return sorted.filter((t) => t.status === statusFilter)
  }, [traces, statusFilter])

  const displayLimit =
    displayLimitState.filter === statusFilter ? displayLimitState.limit : PAGE_SIZE

  const visibleTraces = useMemo(
    () => filteredTraces.slice(0, displayLimit),
    [filteredTraces, displayLimit],
  )
  const hasMore = filteredTraces.length > visibleTraces.length

  const selectedTrace = useMemo(
    () => filteredTraces.find((t) => t.trace_id === selectedTraceId) ?? null,
    [filteredTraces, selectedTraceId],
  )

  function handleStatusFilterChange(nextStatusFilter: StatusFilter): void {
    setStatusFilter(nextStatusFilter)
    setDisplayLimitState({ filter: nextStatusFilter, limit: PAGE_SIZE })
  }

  if (isLoading && traces.length === 0) {
    return <ActivityPanelEmpty body="Loading traces…" />
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <SegmentedControl<StatusFilter>
          value={statusFilter}
          onChange={handleStatusFilterChange}
          options={FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label }))}
          ariaLabel="Trace status filter"
        />
      </div>

      <div
        className={`overflow-y-auto ${selectedTrace ? 'border-b border-border' : 'flex-1'}`}
        style={selectedTrace ? { height: `${topHeight}%` } : undefined}
      >
        {filteredTraces.length === 0 ? (
          <ActivityPanelEmpty
            icon={<TracesEmptyIcon />}
            heading="Traces"
            body={
              statusFilter === 'all'
                ? 'Tool-call traces appear here as agents work'
                : `No ${statusFilter} traces yet`
            }
          />
        ) : (
          <>
            {visibleTraces.map((trace) => (
              <button
                key={trace.trace_id}
                type="button"
                data-testid="trace-row-button"
                className={`pipeline-exec-row${selectedTraceId === trace.trace_id ? ' pipeline-exec-row--active' : ''}`}
                onClick={() => setSelectedTraceId(trace.trace_id)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <TraceStatusDot status={trace.status} />
                  <span className="text-sm text-foreground truncate">
                    {trace.root_span_name || 'Unknown span'}
                  </span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] text-muted-foreground tabular-nums">
                    {formatDurationMs(trace.duration_ms)}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {formatTime(trace.timestamp)}
                  </span>
                </div>
              </button>
            ))}
            {hasMore && (
              <button
                type="button"
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors pointer-coarse:min-h-11"
                onClick={() =>
                  setDisplayLimitState({
                    filter: statusFilter,
                    limit: displayLimit + PAGE_SIZE,
                  })
                }
              >
                Load more
              </button>
            )}
          </>
        )}
      </div>

      {selectedTrace && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {selectedTrace && (
        <div className="flex-1 flex flex-col min-h-0">
          <div className="pipeline-detail-header flex items-center gap-3 px-3 border-b border-border">
            <div className="flex items-center gap-2 min-w-0">
              <TraceStatusDot status={selectedTrace.status} />
              <span className="text-xs font-medium text-foreground truncate">
                {selectedTrace.root_span_name || selectedTrace.trace_id.slice(0, 8)}
              </span>
              <span className="text-[10px] text-muted-foreground shrink-0 tabular-nums">
                {formatDurationMs(selectedTrace.duration_ms)}
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {isDetailLoading && spans.length === 0 ? (
              <p className="text-xs text-muted-foreground p-2">Loading spans...</p>
            ) : spans.length === 0 ? (
              <p className="text-xs text-muted-foreground p-2">No spans</p>
            ) : (
              <ul className="traces-tab-spans">
                {spans.map((span) => (
                  <li key={span.id} className="traces-tab-span">
                    <TraceStatusDot status={span.status} />
                    <span className="traces-tab-span__name" title={span.name}>
                      {span.name}
                    </span>
                    <span className="traces-tab-span__duration tabular-nums">
                      {formatDurationNs(Math.max(0, span.end_time_ns - span.start_time_ns))}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  )
})

function TraceStatusDot({ status }: { status: SpanRecord['status'] | TraceRecord['status'] }) {
  if (status === 'OK') {
    return (
      <ActivityRowStatusDot
        color="var(--color-success-foreground)"
        label="OK"
      />
    )
  }
  if (status === 'ERROR') {
    return <ActivityRowStatusDot color="var(--color-error)" label="Error" />
  }
  return <ActivityRowStatusDot color="var(--text-muted)" label="Unset" />
}

function formatDurationMs(ms: number): string {
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`
  return `${(ms / 60_000).toFixed(1)}m`
}

function formatDurationNs(ns: number): string {
  return formatDurationMs(ns / 1_000_000)
}
