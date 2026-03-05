import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: vi.fn(),
}))

import { useTasks } from '../useTasks'

let mockFetch: MockFetchInstance

const SAMPLE_TASKS = [
  {
    id: 'task-1',
    ref: '#100',
    title: 'Fix bug',
    status: 'open',
    priority: 1,
    type: 'task',
    parent_task_id: null,
    created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T12:00:00Z',
    seq_num: 100,
    path_cache: '100',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
  {
    id: 'task-2',
    ref: '#101',
    title: 'Add feature',
    status: 'open',
    priority: 2,
    type: 'task',
    parent_task_id: null,
    created_at: '2026-03-02T00:00:00Z',
    updated_at: '2026-03-02T12:00:00Z',
    seq_num: 101,
    path_cache: '101',
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: 'proj-1',
  },
]

const TASK_LIST_RESPONSE = {
  tasks: SAMPLE_TASKS,
  total: 2,
  stats: { open: 2, closed: 0 },
  limit: 200,
  offset: 0,
}

beforeEach(() => {
  mockFetch = createMockFetch()
  // Use regex to match ONLY the list endpoint (with query params), not /api/tasks/<id>
  mockFetch.mockJsonResponse(/\/api\/tasks\?/, TASK_LIST_RESPONSE)
})

afterEach(() => {
  mockFetch.restore()
  vi.restoreAllMocks()
})

describe('useTasks', () => {
  it('fetches tasks on mount', async () => {
    const { result } = renderHook(() => useTasks())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.tasks).toHaveLength(2)
    expect(result.current.total).toBe(2)
    expect(result.current.stats).toEqual({ open: 2, closed: 0 })
  })

  it('defaults to open status filter', async () => {
    const { result } = renderHook(() => useTasks())

    expect(result.current.filters.status).toBe('open')

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    // Should include status=open in the fetch URL
    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('status=open'),
    )
  })

  it('re-fetches when filters change', async () => {
    const { result } = renderHook(() => useTasks())

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    const initialCallCount = mockFetch.fn.mock.calls.length

    act(() => {
      result.current.setFilters(prev => ({ ...prev, status: 'closed' }))
    })

    await waitFor(() => {
      expect(mockFetch.fn.mock.calls.length).toBeGreaterThan(initialCallCount)
    })
  })

  it('getTask fetches a single task detail', async () => {
    const taskDetail = { ...SAMPLE_TASKS[0], description: 'Detailed desc' }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1$/, taskDetail)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.getTask('task-1'))

    expect(task).toBeTruthy()
    expect(task?.description).toBe('Detailed desc')
  })

  it('getTask returns null on failure', async () => {
    mockFetch.mockErrorResponse(/\/api\/tasks\/nonexistent$/, 404)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.getTask('nonexistent'))

    expect(task).toBeNull()
  })

  it('createTask posts and re-fetches', async () => {
    const newTask = { ...SAMPLE_TASKS[0], id: 'task-3', title: 'New task' }
    // POST to /api/tasks (no query params) — register before the list route would match
    mockFetch.mockJsonResponse(/\/api\/tasks$/, newTask)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const created = await act(() =>
      result.current.createTask({ title: 'New task' }),
    )

    expect(created).toBeTruthy()
    expect(created?.title).toBe('New task')
  })

  it('updateTask patches and re-fetches', async () => {
    const updated = { ...SAMPLE_TASKS[0], title: 'Updated' }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1$/, updated)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() =>
      result.current.updateTask('task-1', { title: 'Updated' }),
    )

    expect(task?.title).toBe('Updated')
  })

  it('closeTask posts and re-fetches', async () => {
    const closed = { ...SAMPLE_TASKS[0], status: 'closed' }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/close/, closed)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.closeTask('task-1', 'Done'))

    expect(task?.status).toBe('closed')
  })

  it('reopenTask posts and re-fetches', async () => {
    const reopened = { ...SAMPLE_TASKS[0], status: 'open' }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/reopen/, reopened)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.reopenTask('task-1'))

    expect(task?.status).toBe('open')
  })

  it('deleteTask deletes and re-fetches', async () => {
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1$/, { ok: true })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const ok = await act(() => result.current.deleteTask('task-1'))

    expect(ok).toBe(true)
  })

  it('deleteTask with cascade flag', async () => {
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\?cascade=true/, { ok: true })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(() => result.current.deleteTask('task-1', true))

    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('cascade=true'),
      expect.anything(),
    )
  })

  it('getDependencies returns tree', async () => {
    const tree = { id: 'task-1', blockers: [], blocking: [] }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/dependencies/, tree)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const deps = await act(() => result.current.getDependencies('task-1'))

    expect(deps?.id).toBe('task-1')
  })

  it('getSubtasks returns children', async () => {
    const children = { tasks: [SAMPLE_TASKS[1]], total: 1, stats: {}, limit: 100, offset: 0 }
    // Register specific route — getSubtasks calls /api/tasks?parent_task_id=task-1&limit=100
    // which also matches the general list route. We need a regex that matches parent_task_id
    // but the general route (registered in beforeEach) matches first since it's /\/api\/tasks\?/.
    // Reset and re-register with subtask route first.
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/parent_task_id=task-1/, children)
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, TASK_LIST_RESPONSE)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const subs = await act(() => result.current.getSubtasks('task-1'))

    expect(subs).toHaveLength(1)
  })

  it('handles fetch error gracefully', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockErrorResponse('/api/tasks', 500)

    const { result } = renderHook(() => useTasks())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error).toBeTruthy()
    expect(result.current.tasks).toHaveLength(0)
  })

  it('refreshTasks re-fetches', async () => {
    const { result } = renderHook(() => useTasks())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => result.current.refreshTasks())

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))
  })

  it('maps recently_done filter to status=closed', async () => {
    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, status: 'recently_done' }))
    })

    await waitFor(() => {
      const calls = mockFetch.fn.mock.calls
      const lastCall = calls[calls.length - 1][0] as string
      expect(lastCall).toContain('status=closed')
    })
  })

  it('maps in_review filter to needs_review,review_approved', async () => {
    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, status: 'in_review' }))
    })

    await waitFor(() => {
      const calls = mockFetch.fn.mock.calls
      const lastCall = calls[calls.length - 1][0] as string
      expect(lastCall).toContain('status=needs_review')
    })
  })
})
