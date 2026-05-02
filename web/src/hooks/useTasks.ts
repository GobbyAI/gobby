import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'
import type {
  LifecycleTask,
  ReviewPolicy,
  StageAdvanceAction,
  StageState5,
  StageStateView,
} from '../lib/stageActions'
import { currentStage as selectCurrentStage } from '../lib/stageActions'
import type { CanonicalTaskState, TaskCompatProjection } from '../lib/taskState'
import { countTasksByState, getCanonicalTaskState, matchesTaskStateFilter } from '../lib/taskState'

export type {
  LifecycleTask,
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
  closed_at?: string | null
  closed_in_session_id?: string | null
  escalated_at?: string | null
  pre_escalation_status?: string | null
  category?: string | null
  current_stage: StageStateView | null
  stages: StageStateView[]
}

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

function getBaseUrl(): string {
  return ''
}

type RawTaskPayload = Omit<Partial<GobbyTask>, 'stages' | 'current_stage' | 'state'> & {
  id: string
  title?: string
  type?: string
  stages?: StageStateView[] | null
  current_stage?: StageStateView | null
  state?: Partial<CanonicalTaskState> | null
}

function normalizeTask<T extends RawTaskPayload>(task: T): T & GobbyTask {
  const stages = Array.isArray(task.stages) ? task.stages : []
  const projected = {
    ...task,
    ref: task.ref ?? (task.seq_num != null ? `#${task.seq_num}` : task.id),
    title: task.title ?? '',
    status: task.status ?? 'open',
    priority: task.priority ?? 2,
    task_type: task.task_type ?? task.type ?? 'task',
    parent_task_id: task.parent_task_id ?? null,
    created_at: task.created_at ?? '',
    updated_at: task.updated_at ?? '',
    seq_num: task.seq_num ?? null,
    path_cache: task.path_cache ?? null,
    assignee: task.assignee ?? null,
    agent_name: task.agent_name ?? null,
    sequence_order: task.sequence_order ?? null,
    start_date: task.start_date ?? null,
    due_date: task.due_date ?? null,
    project_id: task.project_id ?? '',
    current_stage: task.current_stage ?? task.state?.current_stage ?? null,
    stages,
  } as GobbyTask
  const current = projected.current_stage ?? selectCurrentStage(projected)
  const canonical = getCanonicalTaskState({
    ...projected,
    current_stage: current,
    state: task.state,
  })

  return {
    ...projected,
    current_stage: canonical.current_stage,
    state: canonical,
  } as T & GobbyTask
}

function normalizeTasks<T extends RawTaskPayload>(tasks: T[] | undefined): Array<T & GobbyTask> {
  return (tasks ?? []).map(task => normalizeTask(task))
}

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
    const newTask = normalizeTask(taskData as unknown as RawTaskPayload)
    if (tasks.some(task => task.id === taskId)) return tasks
    return [...tasks, newTask]
  }

  const updated = taskData as unknown as RawTaskPayload
  return tasks.map(task =>
    task.id === taskId ? normalizeTask({ ...task, ...updated }) : task
  )
}

// =============================================================================
// Hook
// =============================================================================

export function useTasks(projectId?: string | null, pageSize: number = DEFAULT_PAGE_SIZE) {
  const [allTasks, setAllTasks] = useState<GobbyTask[]>([])
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
    async (taskId: string, stageName: string, action: StageAdvanceAction): Promise<void> => {
      await patchStage(taskId, stageName, { action })
    },
    [patchStage]
  )

  const failStage = useCallback(
    async (taskId: string, stageName: string, reason: string): Promise<void> => {
      await patchStage(taskId, stageName, { action: 'fail', reason })
    },
    [patchStage]
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

  const markTaskNeedsReview = useCallback(
    async (taskId: string, notes?: string): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'needs-review', notes ? { notes } : {}),
    [postTaskTransition]
  )

  const markTaskReviewApproved = useCallback(
    async (taskId: string, notes?: string): Promise<GobbyTaskDetail | null> =>
      postTaskTransition(taskId, 'review-approved', notes ? { notes } : {}),
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
        `${baseUrl}/api/tasks?parent_task_id=${encodeURIComponent(taskId)}&limit=100`
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

    setAllTasks(prev => applyTaskEvent(prev, event, taskData))

    // Debounced full refetch to sync stats, total, and filter accuracy
    scheduleRefetch()
  }, [scheduleRefetch])

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
    markTaskNeedsReview,
    markTaskReviewApproved,
    escalateTask,
    deEscalateTask,
    advanceStage,
    failStage,
    startStage,
    closeTask,
    reopenTask,
    deleteTask,
    getDependencies,
    getSubtasks,
    refreshTasks,
  }
}
