import { memo, useState, useEffect, useCallback, useRef, useMemo, type KeyboardEvent } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { PipelineStatusDot, StepDisplay, type StepData } from '../workflows/execution-utils'
import { formatDateTime, formatDuration } from '../workflows/executionFormatters'
import '../workflows/PipelinesPage.css'

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
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [topHeight, setTopHeight] = useState(40)
  const [detailExec, setDetailExec] = useState<PipelineExecution | null>(null)
  const [offset, setOffset] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const filterButtonRefs = useRef<Array<HTMLButtonElement | null>>([])
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

  const selectStatusFilter = useCallback((nextFilter: StatusFilter, focusIndex?: number) => {
    setStatusFilter(nextFilter)
    if (focusIndex != null) {
      queueMicrotask(() => {
        filterButtonRefs.current[focusIndex]?.focus()
      })
    }
  }, [])

  const handleFilterKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      nextIndex = (index + 1) % FILTER_OPTIONS.length
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      nextIndex = (index - 1 + FILTER_OPTIONS.length) % FILTER_OPTIONS.length
    } else if (event.key === 'Home') {
      nextIndex = 0
    } else if (event.key === 'End') {
      nextIndex = FILTER_OPTIONS.length - 1
    }

    if (nextIndex == null) {
      return
    }

    event.preventDefault()
    selectStatusFilter(FILTER_OPTIONS[nextIndex].id, nextIndex)
  }, [selectStatusFilter])

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

  const detailSteps = detailExec?.steps ?? []
  const { total: detailStepCount, passed: passedStepCount, failed: failedStepCount } = useMemo(
    () => {
      const steps = detailExec?.steps ?? []
      return steps.reduce(
        (counts, step) => {
          counts.total += 1
          if (step.status === 'completed' || step.status === 'success') {
            counts.passed += 1
          }
          if (step.status === 'failed' || step.status === 'error') {
            counts.failed += 1
          }
          return counts
        },
        { total: 0, passed: 0, failed: 0 },
      )
    },
    [detailExec?.steps],
  )

  if (loading && executions.length === 0) {
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
              ref={(node) => {
                filterButtonRefs.current[index] = node
              }}
              type="button"
              role="radio"
              aria-checked={statusFilter === option.id}
              tabIndex={statusFilter === option.id ? 0 : -1}
              className={`px-2 py-1 transition-colors ${
                index === 0 ? 'rounded-l-md' : ''
              } ${
                index === FILTER_OPTIONS.length - 1 ? 'rounded-r-md' : ''
              } ${
                statusFilter === option.id
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-muted'
              }`}
              onClick={() => selectStatusFilter(option.id, index)}
              onKeyDown={(event) => handleFilterKeyDown(event, index)}
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
          <div className="pipeline-detail-header flex items-center justify-between gap-3 px-3 border-b border-border">
            <div className="flex items-center gap-2 min-w-0">
              <PipelineStatusDot status={detailExec.status} />
              <span className="text-xs font-medium text-foreground truncate">{detailExec.pipeline_name}</span>
              {detailExec.completed_at && (
                <span className="text-[10px] text-muted-foreground">
                  {formatDuration(detailExec.created_at, detailExec.completed_at)}
                </span>
              )}
            </div>
            {detailStepCount > 0 && (
              <div className="flex items-center gap-3 text-[10px] text-muted-foreground shrink-0">
                <span>{detailStepCount} step{detailStepCount !== 1 ? 's' : ''}</span>
                {passedStepCount > 0 && (
                  <span className="text-green-400">
                    {passedStepCount} passed
                  </span>
                )}
                {failedStepCount > 0 && (
                  <span className="text-red-400">
                    {failedStepCount} failed
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto">
            {detailStepCount > 0 ? (
              <>
                <div className="pipeline-steps-timeline">
                  {detailSteps.map((step, i) => (
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
