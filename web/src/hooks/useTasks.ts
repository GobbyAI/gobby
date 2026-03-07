import { useState, useEffect, useCallback, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

// =============================================================================
// Types
// =============================================================================

export interface GobbyTask {
  id: string
  ref: string
  title: string
  status: string
  priority: number
  task_type: string
  parent_task_id: string | null
  created_at: string
  updated_at: string
  seq_num: number | null
  path_cache: string | null
  requires_user_review: boolean
  assignee: string | null
  agent_name: string | null
  sequence_order: number | null
  start_date: string | null
  due_date: string | null
  project_id: string
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
  status?: string
  priority?: number
  task_type?: string
  assignee?: string
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

export function useTasks() {
  const [tasks, setTasks] = useState<GobbyTask[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<TaskStats>({})
  const [filters, setFilters] = useState<TaskFilters>({
    status: 'open',
    priority: null,
    taskType: null,
    assignee: null,
    label: null,
    parentTaskId: null,
    search: '',
  })
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Fetch tasks list
  const fetchTasks = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({ limit: '200' })
      if (filters.status === 'recently_done') params.set('status', 'closed')
      else if (filters.status === 'in_review') params.set('status', 'needs_review,review_approved')
      else if (filters.status) params.set('status', filters.status)
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
        setTasks(data.tasks || [])
        setTotal(data.total)
        setStats(data.stats || {})
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

  // Close task
  const closeTask = useCallback(
    async (taskId: string, reason?: string): Promise<GobbyTaskDetail | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/close`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason }),
          }
        )
        if (response.ok) {
          const task = await response.json()
          fetchTasks()
          return task
        }
      } catch (e) {
        console.error('Failed to close task:', e)
      }
      return null
    },
    [fetchTasks]
  )

  // Reopen task
  const reopenTask = useCallback(
    async (taskId: string, reason?: string): Promise<GobbyTaskDetail | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(
          `${baseUrl}/api/tasks/${encodeURIComponent(taskId)}/reopen`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason }),
          }
        )
        if (response.ok) {
          const task = await response.json()
          fetchTasks()
          return task
        }
      } catch (e) {
        console.error('Failed to reopen task:', e)
      }
      return null
    },
    [fetchTasks]
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
  handleTaskEventRef.current = (event: string, taskData: Record<string, unknown>) => {
    const taskId = taskData.id as string
    if (!taskId) return

    if (event === 'task_deleted') {
      setTasks(prev => prev.filter(t => t.id !== taskId))
      setTotal(prev => Math.max(0, prev - 1))
    } else if (event === 'task_created') {
      const newTask = taskData as unknown as GobbyTask
      setTasks(prev => {
        if (prev.some(t => t.id === taskId)) return prev
        return [...prev, newTask]
      })
      setTotal(prev => prev + 1)
    } else {
      // task_updated, task_closed, task_reopened
      const updated = taskData as unknown as GobbyTask
      setTasks(prev => prev.map(t => t.id === taskId ? { ...t, ...updated } : t))
    }

    // Debounced full refetch to sync stats, total, and filter accuracy
    if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
    debouncedRefetchRef.current = window.setTimeout(() => fetchTasks(), REFETCH_DEBOUNCE_MS)
  }

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
    closeTask,
    reopenTask,
    deleteTask,
    getDependencies,
    getSubtasks,
    refreshTasks,
  }
}
