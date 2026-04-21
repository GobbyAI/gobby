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
    expect(result.current.stats).toEqual({
      ready: 2,
      in_progress: 0,
      review: 0,
      blocked: 0,
      merge_ready: 0,
      closed: 0,
    })
  })

  it('defaults to no state filter and fetches the unfiltered list', async () => {
    const { result } = renderHook(() => useTasks())

    expect(result.current.filters.status).toBeNull()

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('limit=15'),
    )
    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('offset=0'),
    )
    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('sort_by=updated_at'),
    )
    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('sort_order=desc'),
    )
  })

  it('loadMore fetches the next page with the correct offset and appends results', async () => {
    const FIRST_PAGE = {
      ...TASK_LIST_RESPONSE,
      tasks: SAMPLE_TASKS,
      total: 4,
    }
    const SECOND_PAGE = {
      ...TASK_LIST_RESPONSE,
      tasks: [
        { ...SAMPLE_TASKS[0], id: 'task-3', ref: '#102', seq_num: 102 },
        { ...SAMPLE_TASKS[1], id: 'task-4', ref: '#103', seq_num: 103 },
      ],
      total: 4,
    }
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?[^]*offset=0/, FIRST_PAGE)
    mockFetch.mockJsonResponse(/\/api\/tasks\?[^]*offset=2/, SECOND_PAGE)

    const { result } = renderHook(() => useTasks(undefined, 2))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.allTasks).toHaveLength(2)
    expect(result.current.hasMore).toBe(true)
    expect(result.current.total).toBe(4)

    await act(async () => {
      await result.current.loadMore()
    })

    expect(mockFetch.fn).toHaveBeenCalledWith(
      expect.stringContaining('offset=2'),
    )
    expect(result.current.allTasks).toHaveLength(4)
    expect(result.current.hasMore).toBe(false)
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

  it('maps recently_done to the closed bucket client-side', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      ...TASK_LIST_RESPONSE,
      tasks: [
        SAMPLE_TASKS[0],
        { ...SAMPLE_TASKS[1], id: 'task-closed', status: 'closed', title: 'Closed task' },
      ],
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, status: 'recently_done' }))
    })

    await waitFor(() => {
      expect(result.current.tasks).toHaveLength(1)
      expect(result.current.tasks[0]?.id).toBe('task-closed')
    })
  })

  it('maps in_review to review and merge-ready buckets client-side', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      ...TASK_LIST_RESPONSE,
      tasks: [
        { ...SAMPLE_TASKS[0], id: 'task-review', status: 'needs_review', title: 'Needs review' },
        {
          ...SAMPLE_TASKS[1],
          id: 'task-approved',
          status: 'review_approved',
          title: 'Approved task',
        },
      ],
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, status: 'in_review' }))
    })

    await waitFor(() => {
      expect(result.current.tasks.map(task => task.id)).toEqual(['task-review', 'task-approved'])
    })
  })
})
