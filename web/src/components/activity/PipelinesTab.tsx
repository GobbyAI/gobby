import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { PipelineStatusDot, StepDisplay, type StepData } from '../workflows/execution-utils'
import { formatDateTime, formatDuration } from '../workflows/executionFormatters'
import { DEFAULT_TOP_PANEL_PERCENT } from './constants'
import { ActivityPanelEmpty, PipelinesEmptyIcon } from './ActivityPanelEmpty'
import { ActivityFilterDropdown } from './ActivityFilterDropdown'

interface PipelinesTabProps {
  projectId?: string | null
}

const FILTER_OPTIONS = [
  { id: 'all', label: 'All' },
  { id: 'completed', label: 'Completed' },
  { id: 'failed', label: 'Failed' },
  { id: 'running', label: 'Running' },
] as const

type StatusFilter = typeof FILTER_OPTIONS[number]['id']

interface PipelineExecution {
  id: string
  pipeline_name: string
  status: string
  created_at: string
  completed_at?: string | null
  steps?: StepData[]
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

export const PipelinesTab = memo(function PipelinesTab({ projectId }: PipelinesTabProps) {
  const [executions, setExecutions] = useState<PipelineExecution[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [showFilterDropdown, setShowFilterDropdown] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT)
  const [detailExec, setDetailExec] = useState<PipelineExecution | null>(null)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const selectedIdRef = useRef<string | null>(null)
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
    selectedIdRef.current = selectedId
  }, [selectedId])

  useEffect(() => {
    if (executions.length === 0) {
      if (selectedIdRef.current !== null) {
        selectedIdRef.current = null
        setSelectedId(null)
      }
      if (detailExec !== null) setDetailExec(null)
      return
    }

    const currentSelectedId = selectedIdRef.current
    const nextId =
      currentSelectedId && executions.some((exec) => exec.id === currentSelectedId)
        ? currentSelectedId
        : executions[0].id

    if (currentSelectedId !== nextId) {
      selectedIdRef.current = nextId
      setSelectedId(nextId)
    }
    if (detailExec?.id !== nextId) {
      fetchDetail(nextId)
    }
  }, [detailExec, executions, fetchDetail])

  const {
    steps: detailSteps,
    total: detailStepCount,
    passed: passedStepCount,
    failed: failedStepCount,
  } = useMemo(() => {
    const steps: StepData[] = detailExec?.steps ?? []
    const counts = steps.reduce(
      (acc, step) => {
        acc.total += 1
        if (step.status === 'completed' || step.status === 'success') {
          acc.passed += 1
        }
        if (step.status === 'failed' || step.status === 'error') {
          acc.failed += 1
        }
        return acc
      },
      { total: 0, passed: 0, failed: 0 },
    )
    return { steps, ...counts }
  }, [detailExec?.steps])

  if (loading && executions.length === 0) {
    return <ActivityPanelEmpty body="Loading pipelines…" />
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="activity-panel-toolbar">
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button ml-auto"
          onClick={() => setShowFilterDropdown((v) => !v)}
          title="Filter pipelines"
          aria-label="Filter pipelines"
          aria-expanded={showFilterDropdown}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          <span className="activity-panel-action-btn__label">Filter</span>
          {statusFilter !== 'all' && (
            <span className="activity-filter-badge">1</span>
          )}
        </button>
        {showFilterDropdown && (
          <ActivityFilterDropdown<StatusFilter>
            value={statusFilter}
            options={FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label }))}
            onChange={setStatusFilter}
            onClose={() => setShowFilterDropdown(false)}
            ariaLabel="Pipeline status filter"
          />
        )}
      </div>

      {/* Execution list */}
      <div className={`overflow-y-auto ${selectedId ? 'border-b border-border' : 'flex-1'}`} style={selectedId ? { height: `${topHeight}%` } : undefined}>
        {executions.length === 0 ? (
          <ActivityPanelEmpty
            icon={<PipelinesEmptyIcon />}
            heading="Pipelines"
            body={
              statusFilter === 'all'
                ? 'Pipeline runs appear here when triggered'
                : `No ${statusFilter} pipelines yet`
            }
          />
        ) : (
          <>
            {executions.map((exec) => (
              <button
                type="button"
                key={exec.id}
                className={`pipeline-exec-row${selectedId === exec.id ? ' pipeline-exec-row--active' : ''}`}
                onClick={() => handleSelect(exec.id)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <PipelineStatusDot status={exec.status} />
                  <span className="activity-row-title">{exec.pipeline_name}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="activity-row-meta">
                    {formatDateTime(exec.created_at)}
                  </span>
                </div>
              </button>
            ))}
            {hasMore && (
              <button
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors pointer-coarse:min-h-11"
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
          <div className="h-10 bg-[var(--bg-secondary)] flex items-center justify-between gap-3 px-3 border-b border-border">
            <div className="flex items-center gap-2 min-w-0">
              <PipelineStatusDot status={detailExec.status} />
              <span className="activity-row-title">{detailExec.pipeline_name}</span>
              {detailExec.completed_at && (
                <span className="activity-row-meta">
                  {formatDuration(detailExec.created_at, detailExec.completed_at)}
                </span>
              )}
            </div>
            {detailStepCount > 0 && (
              <div className="flex items-center gap-3 activity-row-meta shrink-0">
                <span>{detailStepCount} step{detailStepCount !== 1 ? 's' : ''}</span>
                {passedStepCount > 0 && (
                  <span className="text-success-foreground">
                    {passedStepCount} passed
                  </span>
                )}
                {failedStepCount > 0 && (
                  <span className="text-error">
                    {failedStepCount} failed
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto">
            {detailStepCount > 0 ? (
              <>
                <div className="flex flex-col py-2 pl-3 pr-2">
                  {detailSteps.map((step, i) => (
                    <StepDisplay
                      key={step.step_id ?? i}
                      step={step}
                      index={i}
                      layout="timeline"
                    />
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

