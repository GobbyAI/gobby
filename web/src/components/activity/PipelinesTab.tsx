import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { SegmentedControl } from '../ui/SegmentedControl'
import { PipelineStatusDot, StepDisplay, type StepData } from '../shared/executions/execution-utils'
import { formatDateTime, formatDuration } from '../shared/executions/executionFormatters'
import { DEFAULT_TOP_PANEL_PERCENT } from './constants'
import { ActivityPanelEmpty, PipelinesEmptyIcon } from './ActivityPanelEmpty'
import { ActivityFilterDropdown } from './ActivityFilterDropdown'
import {
  deletePipelineDefinition,
  exportPipelineYaml,
  loadPipelineDefinitions,
  runPipelineDefinition,
  togglePipelineDefinition,
  updatePipelineDefinition,
  type PipelineDefinition,
  type PipelineDefinitionUpdate,
} from './pipelines/PipelinesDefsActions'
import {
  PipelinesDefsDetail,
  type PipelineDefinitionViewMode,
} from './pipelines/PipelinesDefsDetail'
import { PipelinesDefsList } from './pipelines/PipelinesDefsList'

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

const PIPELINES_SEGMENT_STORAGE_KEY = 'gobby-pipelines-segment-v1'

const PIPELINE_SEGMENT_OPTIONS = [
  { value: 'live', label: 'Live' },
  { value: 'defs', label: 'Defs' },
] as const

type PipelineSegment = typeof PIPELINE_SEGMENT_OPTIONS[number]['value']

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

function readStoredPipelineSegment(): PipelineSegment {
  if (typeof window === 'undefined') return 'live'
  try {
    return window.localStorage.getItem(PIPELINES_SEGMENT_STORAGE_KEY) === 'defs'
      ? 'defs'
      : 'live'
  } catch {
    return 'live'
  }
}

export const PipelinesTab = memo(function PipelinesTab({ projectId }: PipelinesTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const [segment, setSegment] = useState<PipelineSegment>(() => readStoredPipelineSegment())
  const [executions, setExecutions] = useState<PipelineExecution[]>([])
  const [loading, setLoading] = useState(true)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [showFilterDropdown, setShowFilterDropdown] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT)
  const [detailExec, setDetailExec] = useState<PipelineExecution | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [definitions, setDefinitions] = useState<PipelineDefinition[]>([])
  const [definitionsLoading, setDefinitionsLoading] = useState(false)
  const [definitionError, setDefinitionError] = useState<string | null>(null)
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string | null>(null)
  const [definitionTopHeight, setDefinitionTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT)
  const [definitionViewMode, setDefinitionViewMode] =
    useState<PipelineDefinitionViewMode>('detail')
  const [busyDefinitionId, setBusyDefinitionId] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const listRequestIdRef = useRef(0)
  const detailRequestIdRef = useRef(0)
  const selectedIdRef = useRef<string | null>(null)
  const definitionConfirmLeaveRef = useRef<(next: () => void) => void>((next) => next())
  const PAGE_SIZE = 50

  useEffect(() => {
    try {
      window.localStorage.setItem(PIPELINES_SEGMENT_STORAGE_KEY, segment)
    } catch {
      // Storage can be unavailable in restricted browser contexts.
    }
  }, [segment])

  // Fetch executions
  const fetchExecutions = useCallback((options: {
    appendOffset?: number
    limit?: number
    preserveHasMore?: boolean
    signal?: AbortSignal
  } = {}) => {
    const { appendOffset, limit = PAGE_SIZE, preserveHasMore = false, signal } = options
    const requestId = ++listRequestIdRef.current
    const baseUrl = getBaseUrl()
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    if (statusFilter !== 'all') params.set('status', statusFilter)
    params.set('limit', String(limit))
    if (appendOffset !== undefined) params.set('offset', String(appendOffset))
    return fetch(`${baseUrl}/api/pipelines/executions?${params}`, { signal })
      .then((res) => (res.ok ? res.json() : { executions: [] }))
      .then((data) => {
        if (signal?.aborted || requestId !== listRequestIdRef.current) return false
        const fetched = data.executions ?? []
        if (appendOffset !== undefined) {
          setExecutions((prev) => [...prev, ...fetched])
        } else {
          setExecutions(fetched)
        }
        if (!preserveHasMore) setHasMore(fetched.length === limit)
        return true
      })
      .catch((err) => {
        if (signal?.aborted || requestId !== listRequestIdRef.current) return false
        if (err instanceof DOMException && err.name === 'AbortError') return false
        if (appendOffset === undefined && !preserveHasMore) setExecutions([])
        return false
      })
  }, [projectId, statusFilter])

  // Reset offset and reload when filter changes
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchExecutions({ signal: controller.signal }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [fetchExecutions])

  const handleLoadMore = useCallback(() => {
    const nextOffset = executions.length
    setLoadingMore(true)
    fetchExecutions({ appendOffset: nextOffset }).finally(() => {
      setLoadingMore(false)
    })
  }, [executions.length, fetchExecutions])

  // Fetch detail for selected execution
  const fetchDetail = useCallback((id: string) => {
    const requestId = ++detailRequestIdRef.current
    const baseUrl = getBaseUrl()
    fetch(`${baseUrl}/api/pipelines/${id}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const execution = data?.execution ?? data
        if (
          requestId === detailRequestIdRef.current
          && execution?.id === selectedIdRef.current
        ) {
          setDetailExec(execution)
        }
      })
      .catch((err) => {
        if (requestId === detailRequestIdRef.current) {
          console.error('Failed to fetch pipeline detail:', err)
        }
      })
  }, [])

  // Poll running executions
  useEffect(() => {
    const hasRunning = executions.some((e) => e.status === 'running')
    if (hasRunning || (selectedId && detailExec?.status === 'running')) {
      pollRef.current = setInterval(() => {
        fetchExecutions({ limit: executions.length || PAGE_SIZE, preserveHasMore: true })
        if (selectedId) fetchDetail(selectedId)
      }, 3000)
    }
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [executions, selectedId, detailExec?.status, fetchExecutions, fetchDetail])

  const handleSelect = useCallback((id: string) => {
    selectedIdRef.current = id
    setSelectedId(id)
    fetchDetail(id)
  }, [fetchDetail])

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

  const refreshDefinitions = useCallback(async () => {
    setDefinitionsLoading(true)
    setDefinitionError(null)
    try {
      const data = await loadPipelineDefinitions(projectId)
      setDefinitions(data)
      setSelectedDefinitionId((current) =>
        current && data.some((definition) => definition.id === current)
          ? current
          : data[0]?.id ?? null,
      )
      return data
    } catch (error) {
      setDefinitionError(
        error instanceof Error ? error.message : 'Failed to load pipeline definitions',
      )
      setDefinitions([])
      setSelectedDefinitionId(null)
      return []
    } finally {
      setDefinitionsLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    if (segment !== 'defs') return
    void refreshDefinitions()
  }, [refreshDefinitions, segment])

  const selectedDefinition = useMemo(
    () => definitions.find((definition) => definition.id === selectedDefinitionId) ?? null,
    [definitions, selectedDefinitionId],
  )

  const replaceDefinition = useCallback((updated: PipelineDefinition) => {
    setDefinitions((current) =>
      current.map((definition) => (definition.id === updated.id ? updated : definition)),
    )
    setSelectedDefinitionId(updated.id)
  }, [])

  const handleSegmentChange = useCallback((next: PipelineSegment) => {
    if (next === segment) return
    const changeSegment = () => {
      setSegment(next)
      if (next === 'defs') setDefinitionViewMode('detail')
    }
    if (segment === 'defs') {
      definitionConfirmLeaveRef.current(changeSegment)
      return
    }
    changeSegment()
  }, [segment])

  const handleDefinitionSelect = useCallback((definition: PipelineDefinition) => {
    definitionConfirmLeaveRef.current(() => {
      setSelectedDefinitionId(definition.id)
      setDefinitionViewMode('detail')
    })
  }, [])

  const handleSaveDefinition = useCallback(async (draft: PipelineDefinition) => {
    const updated = await updatePipelineDefinition(draft.id, {
      description: draft.description ?? '',
      enabled: draft.enabled,
      tags: draft.tags ?? [],
    })
    if (!updated) return false
    replaceDefinition(updated)
    return true
  }, [replaceDefinition])

  const handleUpdateDefinition = useCallback(async (
    id: string,
    updates: PipelineDefinitionUpdate,
  ) => {
    setDefinitionError(null)
    const updated = await updatePipelineDefinition(id, updates)
    if (!updated) {
      throw new Error('Failed to update pipeline definition')
    }
    replaceDefinition(updated)
    return updated
  }, [replaceDefinition])

  const withDefinitionBusy = useCallback(async (
    definition: PipelineDefinition,
    action: () => Promise<void>,
  ) => {
    setBusyDefinitionId(definition.id)
    setDefinitionError(null)
    try {
      await action()
    } catch (error) {
      setDefinitionError(error instanceof Error ? error.message : String(error))
    } finally {
      setBusyDefinitionId(null)
    }
  }, [])

  const handleRunDefinition = useCallback((definition: PipelineDefinition) => {
    void withDefinitionBusy(definition, async () => {
      const ran = await runPipelineDefinition(definition.name, projectId)
      if (!ran) throw new Error(`Failed to run ${definition.name}`)
    })
  }, [projectId, withDefinitionBusy])

  const handleToggleDefinition = useCallback((definition: PipelineDefinition) => {
    void withDefinitionBusy(definition, async () => {
      const updated = await togglePipelineDefinition(definition.id)
      if (!updated) throw new Error(`Failed to update ${definition.name}`)
      replaceDefinition(updated)
    })
  }, [replaceDefinition, withDefinitionBusy])

  const handleDeleteDefinition = useCallback((definition: PipelineDefinition) => {
    void withDefinitionBusy(definition, async () => {
      const shouldDelete = await confirm({
        title: `Delete "${definition.name}"?`,
        confirmLabel: 'Delete',
        destructive: true,
      })
      if (!shouldDelete) return
      const deleted = await deletePipelineDefinition(definition.id)
      if (!deleted) throw new Error(`Failed to delete ${definition.name}`)
      const remainingDefinitions = definitions.filter((item) => item.id !== definition.id)
      setDefinitions(remainingDefinitions)
      setSelectedDefinitionId((current) =>
        current === definition.id ? remainingDefinitions[0]?.id ?? null : current,
      )
      setDefinitionViewMode('detail')
    })
  }, [confirm, definitions, withDefinitionBusy])

  const handleExportDefinition = useCallback((definition: PipelineDefinition) => {
    void withDefinitionBusy(definition, async () => {
      const yaml = await exportPipelineYaml(definition.id)
      if (!yaml) throw new Error(`Failed to export ${definition.name}`)
      const blob = new Blob([yaml], { type: 'application/x-yaml' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${definition.name}.yaml`
      link.click()
      URL.revokeObjectURL(url)
    })
  }, [withDefinitionBusy])

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

  return (
    <div className="flex flex-col h-full">
      {ConfirmDialogElement}
      {/* Toolbar */}
      <div className="activity-panel-toolbar">
        <SegmentedControl<PipelineSegment>
          options={PIPELINE_SEGMENT_OPTIONS}
          value={segment}
          onChange={handleSegmentChange}
          ariaLabel="Pipeline surface"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
        {segment === 'live' && (
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
        )}
        {segment === 'live' && showFilterDropdown && (
          <ActivityFilterDropdown<StatusFilter>
            value={statusFilter}
            options={FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label }))}
            onChange={setStatusFilter}
            onClose={() => setShowFilterDropdown(false)}
            ariaLabel="Pipeline status filter"
          />
        )}
      </div>

      {segment === 'defs' ? (
        <>
          {definitionError && (
            <button
              type="button"
              className="border-b border-border bg-error-soft px-3 py-2 text-left text-sm text-error"
              onClick={() => setDefinitionError(null)}
              aria-label={`Dismiss error: ${definitionError}`}
            >
              {definitionError}
            </button>
          )}
          <div className="flex min-h-0 flex-1 flex-col">
            <div
              className={`overflow-y-auto ${selectedDefinition ? 'border-b border-border' : 'flex-1'}`}
              style={selectedDefinition ? { height: `${definitionTopHeight}%` } : undefined}
            >
              {definitionsLoading && definitions.length === 0 ? (
                <ActivityPanelEmpty
                  icon={<PipelinesEmptyIcon />}
                  heading="Pipeline definitions"
                  body="Loading pipeline definitions..."
                />
              ) : definitions.length === 0 ? (
                <ActivityPanelEmpty
                  icon={<PipelinesEmptyIcon />}
                  heading="Pipeline definitions"
                  body="Pipeline definitions appear here when they are installed or created."
                />
              ) : (
                <PipelinesDefsList
                  definitions={definitions}
                  selectedId={selectedDefinitionId}
                  busyId={busyDefinitionId}
                  onSelect={handleDefinitionSelect}
                  onRun={handleRunDefinition}
                  onToggle={handleToggleDefinition}
                  onDelete={handleDeleteDefinition}
                />
              )}
            </div>
            {selectedDefinition && (
              <ResizeHandle
                direction="vertical"
                onResize={setDefinitionTopHeight}
                panelHeight={definitionTopHeight}
                minHeight={25}
                maxHeight={75}
              />
            )}
            {selectedDefinition && (
              <div className="min-h-0 flex-1">
                <PipelinesDefsDetail
                  key={selectedDefinition.id}
                  definition={selectedDefinition}
                  viewMode={definitionViewMode}
                  onViewModeChange={setDefinitionViewMode}
                  onSave={handleSaveDefinition}
                  onUpdateDefinition={handleUpdateDefinition}
                  onExport={handleExportDefinition}
                  onError={setDefinitionError}
                  onConfirmLeaveChange={(handler) => {
                    definitionConfirmLeaveRef.current = handler
                  }}
                />
              </div>
            )}
          </div>
        </>
      ) : (
        <>
      {/* Execution list */}
      <div className={`overflow-y-auto ${selectedId ? 'border-b border-border' : 'flex-1'}`} style={selectedId ? { height: `${topHeight}%` } : undefined}>
        {loading && executions.length === 0 ? (
          <ActivityPanelEmpty body="Loading pipelines…" />
        ) : executions.length === 0 ? (
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
        </>
      )}
    </div>
  )
})
