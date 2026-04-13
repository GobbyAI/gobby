import { useState, useEffect, useCallback, useRef, useMemo } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'
import type { CanonicalTaskState, TaskCompatProjection } from '../lib/taskState'
import { countTasksByBucket, matchesTaskBucketFilter } from '../lib/taskState'

// =============================================================================
// Types
// =============================================================================

export interface GobbyTask {
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
  lifecycle_stage?: string | null
  closed_at?: string | null
  closed_in_session_id?: string | null
  escalated_at?: string | null
  pre_escalation_status?: string | null
  category?: string | null
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
}

export interface TaskFilters {
  status: string | null
  priority: number | null
  taskType: string | null
  assignee: string | null
  label: string | null
  parentTaskId: string | null
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

function getBaseUrl(): string {
  return ''
}

// =============================================================================
// Hook
// =============================================================================

export function useTasks(projectId?: string | null) {
  const [allTasks, setAllTasks] = useState<GobbyTask[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filters, setFilters] = useState<TaskFilters>({
    status: null,
    priority: null,
    taskType: null,
    assignee: null,
    label: null,
    parentTaskId: null,
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

  // Fetch tasks list
  const fetchTasks = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({ limit: '500' })
      if (filters.priority !== null) params.set('priority', String(filters.priority))
      if (filters.taskType) params.set('task_type', filters.taskType)
      if (filters.assignee) params.set('assignee', filters.assignee)
      if (filters.label) params.set('label', filters.label)
      if (filters.parentTaskId) params.set('parent_task_id', filters.parentTaskId)
      if (filters.search) params.set('search', filters.search)
      if (filters.projectId) params.set('project_id', filters.projectId)

      const response = await fetch(`${baseUrl}/api/tasks?${params}`)
      if (response.ok) {
        const data: TaskListResponse = await response.json()
        setAllTasks(data.tasks || [])
        setError(null)
      } else {
        setError(`Failed to fetch tasks (${response.status})`)
      }
    } catch (e) {
      console.error('Failed to fetch tasks:', e)
      setError('Failed to fetch tasks')
    } finally {
      setIsLoading(false)
    }
  }, [filters])

  const tasks = useMemo(
    () => allTasks.filter(task => matchesTaskBucketFilter(task, filters.status)),
    [allTasks, filters.status]
  )

  const total = tasks.length
  const stats = useMemo(() => countTasksByBucket(allTasks), [allTasks])

  // Get single task detail
  const getTask = useCallback(async (taskId: string): Promise<GobbyTaskDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/tasks/${encodeURIComponent(taskId)}`)
      if (response.ok) {
        return await response.json()
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
          const task = await response.json()
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
          const task = await response.json()
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
          const task = await response.json()
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
        return data.tasks || []
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

  // Use a ref for the event handler to avoid stale closures in the WS callback
  const handleTaskEventRef = useRef<(event: string, taskData: Record<string, unknown>) => void>(() => {})
  const handleTaskEvent = useCallback((event: string, taskData: Record<string, unknown>) => {
    const taskId = taskData.id as string
    if (!taskId) return

    if (event === 'task_deleted') {
      setAllTasks(prev => prev.filter(t => t.id !== taskId))
    } else if (event === 'task_created') {
      const newTask = taskData as unknown as GobbyTask
      setAllTasks(prev => {
        if (prev.some(t => t.id === taskId)) return prev
        return [...prev, newTask]
      })
    } else {
      // task_updated, task_closed, task_reopened
      const updated = taskData as unknown as GobbyTask
      setAllTasks(prev => prev.map(t => t.id === taskId ? { ...t, ...updated } : t))
    }

    // Debounced full refetch to sync stats, total, and filter accuracy
    if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
    debouncedRefetchRef.current = window.setTimeout(() => fetchTasks(), REFETCH_DEBOUNCE_MS)
  }, [fetchTasks])

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

  const refreshTasks = useCallback(() => {
    setIsLoading(true)
    fetchTasks()
  }, [fetchTasks])

  return {
    allTasks,
    tasks,
    total,
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
    closeTask,
    reopenTask,
    deleteTask,
    getDependencies,
    getSubtasks,
    refreshTasks,
  }
}
