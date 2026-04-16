import { memo, useState, useEffect, useCallback, useRef } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { PipelineStatusDot, StepDisplay, type StepData } from '../workflows/execution-utils'
import { formatDateTime, formatDuration } from '../workflows/executionFormatters'
import '../workflows/PipelinesPage.css'

interface PipelinesTabProps {
  projectId?: string | null
}

interface PipelineExecution {
  id: string
  pipeline_name: string
  status: string
  created_at: string
  completed_at?: string | null
  steps?: StepData[]
}

const FILTER_OPTIONS = [
  { id: 'all', label: 'All' },
  { id: 'completed', label: 'Completed' },
  { id: 'failed', label: 'Failed' },
  { id: 'running', label: 'Running' },
] as const

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

export const PipelinesTab = memo(function PipelinesTab({ projectId }: PipelinesTabProps) {
  const [executions, setExecutions] = useState<PipelineExecution[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<'all' | 'completed' | 'failed' | 'running'>('all')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [topHeight, setTopHeight] = useState(40)
  const [detailExec, setDetailExec] = useState<PipelineExecution | null>(null)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const PAGE_SIZE = 50

  // Fetch executions
  const fetchExecutions = useCallback((appendOffset?: number, signal?: AbortSignal) => {
    const baseUrl = getBaseUrl()
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    if (statusFilter !== 'all') params.set('status', statusFilter)
    params.set('limit', String(PAGE_SIZE))
    if (appendOffset) params.set('offset', String(appendOffset))
    return fetch(`${baseUrl}/api/pipelines/executions?${params}`, { signal })
      .then((res) => (res.ok ? res.json() : { executions: [] }))
      .then((data) => {
        const fetched = data.executions ?? []
        if (appendOffset) {
          setExecutions((prev) => [...prev, ...fetched])
        } else {
          setExecutions(fetched)
        }
        setHasMore(fetched.length === PAGE_SIZE)
      })
      .catch((err) => {
        if (err instanceof DOMException && err.name === 'AbortError') return
        if (!appendOffset) setExecutions([])
      })
  }, [projectId, statusFilter])

  // Reset offset and reload when filter changes
  useEffect(() => {
    const controller = new AbortController()
    setOffset(0)
    setLoading(true)
    fetchExecutions(undefined, controller.signal).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [fetchExecutions])

  const handleLoadMore = useCallback(() => {
    const nextOffset = offset + PAGE_SIZE
    setLoadingMore(true)
    fetchExecutions(nextOffset).finally(() => {
      setOffset(nextOffset)
      setLoadingMore(false)
    })
  }, [offset, fetchExecutions])

  // Fetch detail for selected execution
  const fetchDetail = useCallback((id: string) => {
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/pipelines/${id}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.execution) setDetailExec(data.execution)
        else if (data?.id) setDetailExec(data)
      })
      .catch((err) => { console.error('Failed to fetch pipeline detail:', err) })
  }, [])

  // Poll running executions
  useEffect(() => {
    const hasRunning = executions.some((e) => e.status === 'running')
    if (hasRunning || (selectedId && detailExec?.status === 'running')) {
      pollRef.current = setInterval(() => {
        fetchExecutions()
        if (selectedId) fetchDetail(selectedId)
      }, 3000)
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [executions, selectedId, detailExec?.status, fetchExecutions, fetchDetail])

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id)
    fetchDetail(id)
  }, [fetchDetail])

  useEffect(() => {
    if (executions.length === 0) {
      if (selectedId !== null) setSelectedId(null)
      if (detailExec !== null) setDetailExec(null)
      return
    }

    if (!selectedId || !executions.some((exec) => exec.id === selectedId)) {
      const nextId = executions[0].id
      setSelectedId(nextId)
      fetchDetail(nextId)
    }
  }, [detailExec, executions, fetchDetail, selectedId])

  if (loading) {
    return <div className="activity-tab-empty"><p>Loading pipelines...</p></div>
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <div className="flex rounded-md border border-border text-xs" role="radiogroup" aria-label="Pipeline status filter">
          {FILTER_OPTIONS.map((option, index) => (
            <button
              key={option.id}
              type="button"
              role="radio"
              aria-checked={statusFilter === option.id}
              className={`px-2 py-1 transition-colors ${
                index === 0 ? 'rounded-l-md' : ''
              } ${
                index === FILTER_OPTIONS.length - 1 ? 'rounded-r-md' : ''
              } ${
                statusFilter === option.id
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-muted'
              }`}
              onClick={() => setStatusFilter(option.id)}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      {/* Execution list */}
      <div className={`overflow-y-auto ${selectedId ? 'border-b border-border' : 'flex-1'}`} style={selectedId ? { height: `${topHeight}%` } : undefined}>
        {executions.length === 0 ? (
          <div className="activity-tab-empty">
            <p>No {statusFilter === 'all' ? '' : statusFilter + ' '}pipelines</p>
            <p className="text-xs text-muted-foreground mt-1">
              Pipeline runs will appear here
            </p>
          </div>
        ) : (
          <>
            {executions.map((exec) => (
              <div
                key={exec.id}
                className={`pipeline-exec-row${selectedId === exec.id ? ' pipeline-exec-row--active' : ''}`}
                onClick={() => handleSelect(exec.id)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <ExecutionStatusIcon status={exec.status} />
                  <span className="text-sm text-foreground truncate">{exec.pipeline_name}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-muted-foreground shrink-0">
                    {formatDateTime(exec.created_at)}
                  </span>
                </div>
              </div>
            ))}
            {hasMore && (
              <button
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
                onClick={handleLoadMore}
                disabled={loadingMore}
              >
                {loadingMore ? 'Loading...' : 'Load more'}
              </button>
            )}
          </>
        )}
      </div>

      {/* Resize handle */}
      {selectedId && detailExec && (
        <ResizeHandle direction="vertical" onResize={setTopHeight} panelHeight={topHeight} minHeight={15} maxHeight={80} />
      )}

      {/* Detail pane */}
      {selectedId && detailExec && (
        <div className="flex-1 flex flex-col min-h-0">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-border bg-muted/30">
            <div className="flex items-center gap-2 min-w-0">
              <PipelineStatusDot status={detailExec.status} />
              <span className="text-xs font-medium text-foreground truncate">{detailExec.pipeline_name}</span>
              {detailExec.completed_at && (
                <span className="text-[10px] text-muted-foreground">
                  {formatDuration(detailExec.created_at, detailExec.completed_at)}
                </span>
              )}
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {detailExec.steps && detailExec.steps.length > 0 ? (
              <>
                <StepSummaryBar steps={detailExec.steps} />
                <div className="pipeline-steps-timeline">
                  {detailExec.steps.map((step, i) => (
                    <StepDisplay key={step.step_id ?? i} step={step} index={i} />
                  ))}
                </div>
              </>
            ) : (
              <p className="text-xs text-muted-foreground p-2">No steps available</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
})

function StepSummaryBar({ steps }: { steps: StepData[] }) {
  const completed = steps.filter((s) => s.status === 'completed' || s.status === 'success').length
  const failed = steps.filter((s) => s.status === 'failed' || s.status === 'error').length
  return (
    <div className="flex items-center gap-3 px-3 py-1.5 text-[10px] text-muted-foreground border-b border-border">
      <span>{steps.length} step{steps.length !== 1 ? 's' : ''}</span>
      {completed > 0 && <span className="text-green-400">{completed} passed</span>}
      {failed > 0 && <span className="text-red-400">{failed} failed</span>}
    </div>
  )
}

function ExecutionStatusIcon({ status }: { status: string }) {
  if (status === 'completed' || status === 'success') {
    return <span className="text-green-400 text-xs">{'\u2713'}</span>
  }
  if (status === 'failed' || status === 'error') {
    return <span className="text-red-400 text-xs">{'\u2717'}</span>
  }
  if (status === 'running') {
    return (
      <span className="pipeline-running-dot" />
    )
  }
  return <span className="text-muted-foreground text-xs">{'\u25CB'}</span>
}
