import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'
import type {
  LifecycleTask,
  ReviewPolicy,
  StageAdvanceAction,
  StageState5,
  StageStateView,
} from '../lib/stageActions'
import { optimisticMoveTaskToStage } from '../lib/stageActions'
import type { CanonicalTaskState, OwnerSessionRef, TaskCompatProjection } from '../lib/taskState'
import { countTasksByState, matchesTaskStateFilter } from '../lib/taskState'
import {
  isRawTaskPayload,
  normalizeTaskPayload,
  normalizeTaskPayloads,
  type RawStagePayload,
  type RawTaskPayload,
} from '../lib/taskNormalization'

export type {
  LifecycleTask,
  OwnerSessionRef,
  ReviewPolicy,
  StageAdvanceAction,
  StageState5,
  StageStateView,
}

// =============================================================================
// Types
// =============================================================================

export interface GobbyTask extends LifecycleTask {
  id: string
  ref: string
  title: string
  status: string
  state?: CanonicalTaskState | null
  compat?: TaskCompatProjection | null
  priority: number
  task_type: string
  parent_task_id: string | null
  created_at: string
  updated_at: string
  seq_num: number | null
  path_cache: string | null
  requires_user_review?: boolean
  assignee: string | null
  agent_name: string | null
  sequence_order: number | null
  start_date: string | null
  due_date: string | null
  project_id: string
  claimed_by_session_id?: string | null
  /**
   * Friendly owner-session identity from the backend serializer. The UI
   * renders `owner_session_ref.ref` (#<seq_num> or short hash), never the
   * raw `claimed_by_session_id` UUID. Optional only for older payloads.
   */
  owner_session_ref?: OwnerSessionRef | null
  closed_at?: string | null
  closed_in_session_id?: string | null
  escalated_at?: string | null
  pre_escalation_status?: string | null
  category?: string | null
  current_stage: StageStateView | null
  stages: StageStateView[]
  allow_automation?: boolean | null
  yolo?: boolean | null
  isolation?: string | null
  dispatch_failure_count?: number | null
  additional_skills?: string[] | null
  assigned_agent?: string | null
  /**
   * Definitive build lifecycle state from the backend (allow_automation +
   * durable `gobby build` lifecycle event). Replaces the old client-side
   * hasBuildEvidence heuristic. Optional only for resilience against an
   * older payload; the serializer always populates it.
   */
  build_state?: BuildState | null
}

export type BuildState = 'never_started' | 'running' | 'paused'

export interface GobbyTaskDetail extends GobbyTask {
  description: string | null
  assignee: string | null
  labels: string[] | null
  category: string | null
  validation_status: string | null
  validation_feedback: string | null
  validation_criteria: string | null
  validation_fail_count: number
  validation_override_reason: string | null
  closed_at: string | null
  closed_reason: string | null
  closed_commit_sha: string | null
  commits: string[] | null
  escalated_at: string | null
  escalation_reason: string | null
  pre_escalation_status?: string | null
  created_in_session_id: string | null
  closed_in_session_id: string | null
  complexity_score: number | null
  is_expanded: boolean
  expansion_status: string
  github_pr_number: number | null
  github_repo: string | null
  allow_automation?: boolean | null
  yolo?: boolean | null
  isolation?: string | null
  dispatch_failure_count?: number | null
  additional_skills?: string[] | null
  assigned_agent?: string | null
}

export interface TaskFilters {
  status: string | null
  priority: number | null
  taskType: string | null
  assignee: string | null
  label: string | null
  parentTaskId: string | null
  stage: string | null
  stageState: StageState5 | null
  search: string
  projectId?: string | null
}

export interface TaskStats {
  [status: string]: number
}

export interface TaskListResponse {
  tasks: GobbyTask[]
  total: number
  stats: TaskStats
  limit: number
  offset: number
}

export interface DependencyTree {
  id: string
  /** Friendly ref (#<seq_num> or short hash), resolved by the deps route. */
  ref?: string
  title?: string
  task_type?: string
  blockers?: DependencyTree[]
  blocking?: DependencyTree[]
  _truncated?: boolean
}

interface CreateTaskParams {
  title: string
  description?: string
  priority?: number
  task_type?: string
  parent_task_id?: string
  labels?: string[]
  category?: string
  validation_criteria?: string
  assignee?: string
}

interface UpdateTaskParams {
  title?: string
  description?: string
  priority?: number
  task_type?: string
  labels?: string[]
  parent_task_id?: string
  category?: string
  validation_criteria?: string
  sequence_order?: number
}

// =============================================================================
// Helpers
// =============================================================================

const REFETCH_DEBOUNCE_MS = 500
const DEFAULT_PAGE_SIZE = 15
const moveTaskLocks = new Map<string, Promise<void>>()

function getBaseUrl(): string {
  return ''
}

const normalizeTask = normalizeTaskPayload
const normalizeTasks = normalizeTaskPayloads

function setQueryParam(
  params: URLSearchParams,
  key: string,
  value: string | number | null | undefined,
): void {
  if (value === null || value === undefined || value === '') return
  params.set(key, String(value))
}

function buildTaskListParams(filters: TaskFilters, offset: number, limit: number): URLSearchParams {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  params.set('sort_by', 'updated_at')
  params.set('sort_order', 'desc')
  params.set('include_stages', '1')
  setQueryParam(params, 'priority', filters.priority)
  setQueryParam(params, 'task_type', filters.taskType)
  setQueryParam(params, 'assignee', filters.assignee)
  setQueryParam(params, 'label', filters.label)
  setQueryParam(params, 'parent_task_id', filters.parentTaskId)
  setQueryParam(params, 'stage', filters.stage)
  setQueryParam(params, 'stage_state', filters.stageState)
  setQueryParam(params, 'search', filters.search)
  setQueryParam(params, 'project_id', filters.projectId)
  return params
}

function appendIncomingTasks(current: GobbyTask[], incoming: GobbyTask[]): GobbyTask[] {
  const seen = new Set(current.map(task => task.id))
  const merged = current.slice()
  for (const task of incoming) {
    if (!seen.has(task.id)) merged.push(task)
  }
  return merged
}

function previewTaskEventPayload(taskData: Record<string, unknown>): Record<string, unknown> {
  const safeKeys = [
    'id',
    'ref',
    'seq_num',
    'status',
    'state',
    'task_type',
    'parent_task_id',
    'current_stage',
    'stages',
    'owner_session_ref',
    'claimed_by_session_id',
    'created_at',
    'updated_at',
    'project_id',
  ] as const
  return Object.fromEntries(
    safeKeys.flatMap(key => taskData[key] === undefined ? [] : [[key, taskData[key]]])
  )
}

function warnInvalidTaskPayload(
  event: string,
  taskId: unknown,
  taskData: Record<string, unknown>,
): void {
  console.warn('Ignoring invalid task event payload', {
    event,
    taskId,
    taskData: previewTaskEventPayload(taskData),
  })
}

function applyTaskEvent(
  tasks: GobbyTask[],
  event: string,
  taskData: Record<string, unknown>,
): GobbyTask[] {
  const taskId = taskData.id as string
  if (event === 'task_deleted') {
    return tasks.filter(task => task.id !== taskId)
  }
  if (event === 'task_created') {
    if (!isRawTaskPayload(taskData)) {
      warnInvalidTaskPayload(event, taskId, taskData)
      return tasks
    }
    const newTask = normalizeTask(taskData)
    if (tasks.some(task => task.id === taskId)) return tasks
    return [...tasks, newTask]
  }

  let matched = false
  const nextTasks = tasks.map(task => {
    if (task.id !== taskId) return task
    matched = true
    const merged = { ...task, ...taskData }
    if (!isRawTaskPayload(merged)) {
      warnInvalidTaskPayload(event, taskId, taskData)
      return task
    }
    return normalizeTask(merged)
  })
  if (matched || !isRawTaskPayload(taskData)) return nextTasks
  return [...nextTasks, normalizeTask(taskData)]
}

function stageMutationErrorMessage(error: unknown, fallback: string): string {
  if (typeof error === 'object' && error !== null) {
    const record = error as { detail?: unknown; reason?: unknown; error?: unknown }
    for (const value of [record.detail, record.reason, record.error]) {
      if (typeof value === 'string' && value.trim()) return value
    }
  }
  if (error instanceof Error && error.message.trim()) return error.message
  return fallback
}

function normalizeTaskWithStageRows(task: GobbyTask, stages: RawStagePayload[]): GobbyTask {
  const payload: RawTaskPayload = {
    ...task,
    current_stage: null,
    state: task.state ? { ...task.state, current_stage: null } : null,
    stages,
  }
  return normalizeTask(payload)
}

function serializeTaskMove(taskId: string, action: () => Promise<void>): Promise<void> {
  const previous = moveTaskLocks.get(taskId) ?? Promise.resolve()
  const next = previous.catch(() => undefined).then(action)
  const barrier = next.catch(() => undefined).finally(() => {
    if (moveTaskLocks.get(taskId) === barrier) moveTaskLocks.delete(taskId)
  })
  moveTaskLocks.set(taskId, barrier)
  return next
}

// =============================================================================
// Hook
// =============================================================================

export function useTasks(projectId?: string | null, pageSize: number = DEFAULT_PAGE_SIZE) {
  const [allTasks, setAllTasks] = useState<GobbyTask[]>([])
  const allTasksRef = useRef<GobbyTask[]>([])
  const [serverTotal, setServerTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const requestVersionRef = useRef(0)
  const [filters, setFilters] = useState<TaskFilters>({
    status: null,
    priority: null,
    taskType: null,
    assignee: null,
    label: null,
    parentTaskId: null,
    stage: null,
    stageState: null,
    search: '',
    projectId: projectId ?? null,
  })

  // Keep projectId in sync when prop changes
  useEffect(() => {
    setFilters(f => {
      const newId = projectId ?? null
      if (f.projectId === newId) return f
      return { ...f, projectId: newId }
    })
  }, [projectId])

  const buildParams = useCallback((offset: number, limit: number) => {
    return buildTaskListParams(filters, offset, limit)
  }, [filters])

  // Fetch first page (replaces accumulated tasks)
  const fetchTasks = useCallback(async () => {
    const requestVersion = ++requestVersionRef.current
    try {
      const baseUrl = getBaseUrl()
      const params = buildParams(0, pageSize)
      const response = await fetch(`${baseUrl}/api/tasks?${params}`)
      if (requestVersionRef.current !== requestVersion) return
      if (response.ok) {
        const data: TaskListResponse = await response.json()
        setAllTasks(normalizeTasks(data.tasks))
        setServerTotal(data.total ?? (data.tasks?.length ?? 0))
        setError(null)
      } else {
        setError(`Failed to fetch tasks (${response.status})`)
      }
    } catch (e) {
      if (requestVersionRef.current !== requestVersion) return
      console.error('Failed to fetch tasks:', e)
      setError('Failed to fetch tasks')
    } finally {
      if (requestVersionRef.current === requestVersion) {
        setIsLoading(false)
      }
    }
  }, [buildParams, pageSize])

  // Fetch the next page and append to allTasks (used for "Load more")
  const loadMore = useCallback(async () => {
    if (isLoadingMore) return
    const requestVersion = requestVersionRef.current
    setIsLoadingMore(true)
    try {
      const baseUrl = getBaseUrl()
      const params = buildParams(allTasks.length, pageSize)
      const response = await fetch(`${baseUrl}/api/tasks?${params}`)
      if (requestVersionRef.current !== requestVersion) return
      if (response.ok) {
        const data: TaskListResponse = await response.json()
        const incoming = normalizeTasks(data.tasks)
        if (incoming.length > 0) {
          setAllTasks(prev => appendIncomingTasks(prev, incoming))
        }
        setServerTotal((prev) => data.total ?? prev)
        setError(null)
      } else {
        setError(`Failed to load more tasks (${response.status})`)
      }
    } catch (e) {
      if (requestVersionRef.current !== requestVersion) return
      console.error('Failed to load more tasks:', e)
      setError('Failed to load more tasks')
    } finally {
      setIsLoadingMore(false)
    }
  }, [allTasks.length, buildParams, isLoadingMore, pageSize])

  const tasks = useMemo(
    () => allTasks.filter(task => matchesTaskStateFilter(task, filters.status)),
    [allTasks, filters.status]
  )

  const total = serverTotal
  const hasMore = allTasks.length < serverTotal
  const stats = useMemo(() => countTasksByState(allTasks), [allTasks])

  useEffect(() => {
    allTasksRef.current = allTasks
  }, [allTasks])

  // Get single task detail
  const getTask = useCallback(async (taskId: string): Promise<GobbyTaskDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}`)
      if (response.ok) {
        return normalizeTask(await response.json())
      }
    } catch (e) {
      console.error('Failed to get task:', e)
    }
    return null
  }, [])

  // Create task
  const createTask = useCallback(
    async (params: CreateTaskParams): Promise<GobbyTaskDetail | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/tasks`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const task = normalizeTask(await response.json())
          fetchTasks()
          return task
        }
      } catch (e) {
        console.error('Failed to create task:', e)
      }
      return null
    },
    [fetchTasks]
  )

  // Update task
  const updateTask = useCallback(
    async (taskId: string, params: UpdateTaskParams): Promise<GobbyTaskDetail | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const task = normalizeTask(await response.json())
          fetchTasks()
          return task
        }
      } catch (e) {
        console.error('Failed to update task:', e)
      }
      return null
    },
    [fetchTasks]
  )

  const postTaskTransition = useCallback(
    async (taskId: string, path: string, body?: Record<string, unknown>): Promise<GobbyTaskDetail | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/${path}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body ?? {}),
          }
        )
        if (response.ok) {
          const task = normalizeTask(await response.json())
          fetchTasks()
          return task
        }
      } catch (e) {
        console.error(`Failed to transition task via ${path}:`, e)
      }
      return null
    },
    [fetchTasks]
  )

  const patchStage = useCallback(
    async (taskId: string, stageName: string, body: Record<string, unknown>): Promise<void> => {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/stages/${encodeURIComponent(stageName)}`,
        {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }
      )
      if (!response.ok) {
        let payload: unknown
        try {
          payload = await response.json()
        } catch {
          payload = { status: response.status, detail: response.statusText }
        }
        throw payload
      }
      fetchTasks()
    },
    [fetchTasks]
  )

  const advanceStage = useCallback(
    async (
      taskId: string,
      stageName: string,
      action: StageAdvanceAction,
      notes?: string,
    ): Promise<void> => {
      if (action === 'reject_review' && !notes) {
        throw new Error('reject_review requires a reason')
      }
      let body: Record<string, unknown> = { action }
      if (action === 'reject_review') {
        body = { action, reason: notes, notes }
      } else if (notes) {
        body = { action, notes }
      }
      await patchStage(taskId, stageName, body)
    },
    [patchStage]
  )

  const failStage = useCallback(
    async (taskId: string, stageName: string, reason: string): Promise<void> => {
      await patchStage(taskId, stageName, { action: 'fail', reason })
    },
    [patchStage]
  )

  const moveTaskToStage = useCallback(
    async (taskId: string, targetStageName: string): Promise<void> => {
      return serializeTaskMove(taskId, async () => {
        setError(null)
        const originalSnapshot = allTasksRef.current.find(task => task.id === taskId) ?? null
        if (originalSnapshot) {
          setAllTasks(prev => {
            const next = prev.map(task =>
              task.id === taskId
                ? normalizeTask(optimisticMoveTaskToStage(task, targetStageName))
                : task
            )
            allTasksRef.current = next
            return next
          })
        }

        try {
          const baseUrl = getBaseUrl()
          const response = await fetch(
            `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/stages/${encodeURIComponent(targetStageName)}`,
            {
              method: 'PATCH',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action: 'move_to' }),
            }
          )
          if (!response.ok) {
            let payload: unknown
            try {
              payload = await response.json()
            } catch {
              payload = { status: response.status, detail: response.statusText }
            }
            throw payload
          }
          const payload = await response.json() as { stages?: RawStagePayload[] }
          if (payload.stages) {
            setAllTasks(prev => {
              const next = prev.map(task =>
                task.id === taskId ? normalizeTaskWithStageRows(task, payload.stages ?? []) : task
              )
              allTasksRef.current = next
              return next
            })
          }
          setError(null)
        } catch (errorPayload) {
          if (originalSnapshot) {
            setAllTasks(prev => {
              const next = prev.map(task => task.id === taskId ? originalSnapshot : task)
              allTasksRef.current = next
              return next
            })
          } else {
            console.warn('Could not rollback task move; previous task snapshot missing', {
              taskId,
              targetStageName,
            })
          }
          setError(stageMutationErrorMessage(errorPayload, 'Failed to move task'))
          throw errorPayload
        }
      })
    },
    // Uses refs and module-level helpers only; keep this stable for board DnD monitors.
    []
  )

  const startStage = useCallback(
    async (taskId: string, stageName: string): Promise<void> => {
      await patchStage(taskId, stageName, { action: 'start' })
    },
    [patchStage]
  )

  const claimTask = useCallback(
    async (taskId: string, sessionId: string, force = false): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'claim', { session_id: sessionId, force }),
    [postTaskTransition]
  )

  const releaseTaskClaim = useCallback(
    async (taskId: string, status?: string): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'release-claim', status ? { status } : {}),
    [postTaskTransition]
  )

  const escalateTask = useCallback(
    async (taskId: string, reason: string): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'escalate', { reason }),
    [postTaskTransition]
  )

  const deEscalateTask = useCallback(
    async (
      taskId: string,
      decisionContext: string,
      targetStatus = 'open',
      resetValidation = false
    ): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'de-escalate', {
        decision_context: decisionContext,
        target_status: targetStatus,
        reset_validation: resetValidation,
      }),
    [postTaskTransition]
  )

  // Close task
  const closeTask = useCallback(
    async (taskId: string, reason?: string): Promise<GobbyTaskDetail | null> => {
      return postTaskTransition(taskId, 'close', reason ? { reason } : {})
    },
    [postTaskTransition]
  )

  // Reopen task
  const reopenTask = useCallback(
    async (taskId: string, reason?: string): Promise<GobbyTaskDetail | null> => {
      return postTaskTransition(taskId, 'reopen', reason ? { reason } : {})
    },
    [postTaskTransition]
  )

  // Delete task
  const deleteTask = useCallback(
    async (taskId: string, cascade = false): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl()
        const params = cascade ? '?cascade=true' : ''
        const response = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}${params}`,
          { method: 'DELETE' }
        )
        if (response.ok) {
          fetchTasks()
          return true
        }
      } catch (e) {
        console.error('Failed to delete task:', e)
      }
      return false
    },
    [fetchTasks]
  )

  // Get dependency tree for a task
  const getDependencies = useCallback(async (taskId: string): Promise<DependencyTree | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/dependencies?direction=both`
      )
      if (response.ok) {
        return await response.json()
      }
    } catch (e) {
      console.error('Failed to get dependencies:', e)
    }
    return null
  }, [])

  // Get subtasks (children of a task)
  const getSubtasks = useCallback(async (taskId: string): Promise<GobbyTask[]> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/tasks?parent_task_id=${encodeURIComponent(taskId)}&limit=100&include_stages=1`
      )
      if (response.ok) {
        const data: TaskListResponse = await response.json()
        return normalizeTasks(data.tasks)
      }
    } catch (e) {
      console.error('Failed to get subtasks:', e)
    }
    return []
  }, [])

  // Fetch on mount and when filters change
  useEffect(() => {
    setIsLoading(true)
    fetchTasks()

    return () => {
      if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
    }
  }, [fetchTasks])

  // -------------------------------------------------------------------------
  // WebSocket: real-time task event subscription
  // -------------------------------------------------------------------------

  const debouncedRefetchRef = useRef<number | null>(null)
  const scheduleRefetch = useCallback(() => {
    if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
    debouncedRefetchRef.current = window.setTimeout(() => fetchTasks(), REFETCH_DEBOUNCE_MS)
  }, [fetchTasks])

  // Use a ref for the event handler to avoid stale closures in the WS callback
  const handleTaskEventRef = useRef<(event: string, taskData: Record<string, unknown>) => void>(() => {})
  const handleTaskEvent = useCallback((event: string, taskData: Record<string, unknown>) => {
    const taskId = taskData.id as string
    if (!taskId) return
    const eventProjectId = taskData.project_id
    if (
      filters.projectId &&
      typeof eventProjectId === 'string' &&
      eventProjectId !== filters.projectId
    ) {
      return
    }

    setAllTasks(prev => applyTaskEvent(prev, event, taskData))

    // Debounced full refetch to sync stats, total, and filter accuracy
    scheduleRefetch()
  }, [filters.projectId, scheduleRefetch])

  useEffect(() => {
    handleTaskEventRef.current = handleTaskEvent
  }, [handleTaskEvent])

  useWebSocketEvent('task_event', useCallback((data: Record<string, unknown>) => {
    if (data.event && (data.task || data.task_id)) {
      handleTaskEventRef.current(
        data.event as string,
        (data.task || { id: data.task_id }) as Record<string, unknown>,
      )
    }
  }, []))

  useWebSocketEvent('stage_changed', useCallback(() => {
    scheduleRefetch()
  }, [scheduleRefetch]))

  const refreshTasks = useCallback(() => {
    setIsLoading(true)
    fetchTasks()
  }, [fetchTasks])

  return {
    allTasks,
    tasks,
    total,
    hasMore,
    isLoadingMore,
    loadMore,
    stats,
    isLoading,
    error,
    filters,
    setFilters,
    getTask,
    createTask,
    updateTask,
    claimTask,
    releaseTaskClaim,
    escalateTask,
    deEscalateTask,
    advanceStage,
    failStage,
    moveTaskToStage,
    startStage,
    closeTask,
    reopenTask,
    deleteTask,
    getDependencies,
    getSubtasks,
    refreshTasks,
  }
}
