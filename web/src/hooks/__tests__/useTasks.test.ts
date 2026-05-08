import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'
import type { StageAdvanceAction } from '../../lib/stageActions'

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: vi.fn(),
}))

import { useTasks } from '../useTasks'
import { useWebSocketEvent } from '../useWebSocketEvent'

let mockFetch: MockFetchInstance
const mockUseWebSocketEvent = vi.mocked(useWebSocketEvent)
const useTasksSourcePath = join(process.cwd(), 'src/hooks/useTasks.ts')

type Phase6TasksApi = ReturnType<typeof useTasks> & {
  advanceStage: (
    taskId: string,
    stageName: string,
    action: StageAdvanceAction,
    notes?: string,
  ) => Promise<unknown>
  failStage: (taskId: string, stageName: string, reason: string) => Promise<unknown>
  startStage: (taskId: string, stageName: string) => Promise<unknown>
}

function installFetchSpy(
  handler: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) {
  const fetchMock = vi.fn(handler)
  globalThis.fetch = fetchMock as unknown as typeof fetch
  window.fetch = fetchMock as unknown as typeof fetch
  return fetchMock
}

function jsonResponse(data: unknown): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function createDeferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function stageState(
  state: 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done' = 'ready',
) {
  return {
    name: 'build',
    display_name: 'Build',
    category: 'delivery',
    state,
    review_policy: 'required',
    updated_at: '2026-05-02T00:00:00Z',
  }
}

function canonicalState(
  state: 'ready' | 'in_progress' | 'needs_review' | 'review_approved' | 'done' = 'ready',
  overrides: Record<string, unknown> = {},
) {
  return {
    current_stage: stageState(state),
    ...overrides,
  }
}

const SAMPLE_TASKS = [
  {
    id: 'task-1',
    ref: '#100',
    title: 'Fix bug',
    state: canonicalState('ready'),
    current_stage: stageState('ready'),
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
    state: canonicalState('ready'),
    current_stage: stageState('ready'),
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
  stats: {
    ready: 2,
    closed: 0,
    in_progress: 0,
    needs_review: 0,
    blocked: 0,
    review_approved: 0,
  },
  limit: 200,
  offset: 0,
}

beforeEach(() => {
  mockUseWebSocketEvent.mockReset()
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
      needs_review: 0,
      blocked: 0,
      review_approved: 0,
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

  it('ignores stale loadMore responses after a refetch replaces the list', async () => {
    mockFetch.restore()

    const firstPage = {
      ...TASK_LIST_RESPONSE,
      tasks: SAMPLE_TASKS,
      total: 3,
    }
    const refreshedPage = {
      ...TASK_LIST_RESPONSE,
      tasks: [{ ...SAMPLE_TASKS[0], id: 'task-refresh', ref: '#104', seq_num: 104 }],
      total: 1,
    }
    const staleLoadMore = {
      ...TASK_LIST_RESPONSE,
      tasks: [{ ...SAMPLE_TASKS[1], id: 'task-stale', ref: '#105', seq_num: 105 }],
      total: 3,
    }

    const deferredLoadMore = createDeferred<Response>()
    let offsetZeroCallCount = 0
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('offset=2')) {
        return deferredLoadMore.promise
      }
      if (url.includes('offset=0')) {
        offsetZeroCallCount += 1
        return Promise.resolve(jsonResponse(offsetZeroCallCount === 1 ? firstPage : refreshedPage))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    globalThis.fetch = fetchMock as unknown as typeof fetch
    window.fetch = fetchMock as unknown as typeof fetch

    const { result } = renderHook(() => useTasks(undefined, 2))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.allTasks.map(task => task.id)).toEqual(['task-1', 'task-2'])

    act(() => {
      void result.current.loadMore()
    })

    await waitFor(() => expect(result.current.isLoadingMore).toBe(true))

    act(() => {
      result.current.refreshTasks()
    })

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.allTasks.map(task => task.id)).toEqual(['task-refresh'])

    await act(async () => {
      deferredLoadMore.resolve(jsonResponse(staleLoadMore))
      await Promise.resolve()
    })

    expect(result.current.allTasks.map(task => task.id)).toEqual(['task-refresh'])
    expect(result.current.total).toBe(1)
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

  it('adds stage filters to task list fetches', async () => {
    const { result } = renderHook(() => useTasks())

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    mockFetch.fn.mockClear()

    act(() => {
      result.current.setFilters(prev => ({
        ...prev,
        stage: 'build',
        stageState: 'needs_review',
      }))
    })

    await waitFor(() => {
      expect(String(mockFetch.fn.mock.calls[0]?.[0])).toContain('stage=build')
      expect(String(mockFetch.fn.mock.calls[0]?.[0])).toContain('stage_state=needs_review')
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
    const closed = {
      ...SAMPLE_TASKS[0],
      state: canonicalState('done', {
        is_closed: true,
        closed_at: '2026-03-03T00:00:00Z',
      }),
      current_stage: stageState('done'),
      closed_at: '2026-03-03T00:00:00Z',
    }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/close/, closed)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.closeTask('task-1', 'Done'))

    expect(task?.status).toBe('closed')
  })

  it('releaseTaskClaim posts to the release-claim route', async () => {
    const released = {
      ...SAMPLE_TASKS[0],
      state: canonicalState('ready'),
      current_stage: stageState('ready'),
    }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/release-claim/, released)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.releaseTaskClaim('task-1'))

    expect(task?.status).toBe('ready')
  })

  it('reopenTask posts and re-fetches', async () => {
    const reopened = {
      ...SAMPLE_TASKS[0],
      state: canonicalState('ready'),
      current_stage: stageState('ready'),
    }
    mockFetch.mockJsonResponse(/\/api\/tasks\/task-1\/reopen/, reopened)

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const task = await act(() => result.current.reopenTask('task-1'))

    expect(task?.status).toBe('ready')
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

  it('maps recently_done to the closed state client-side', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      ...TASK_LIST_RESPONSE,
      tasks: [
        SAMPLE_TASKS[0],
        {
          ...SAMPLE_TASKS[1],
          id: 'task-closed',
          title: 'Closed task',
          state: canonicalState('done', {
            is_closed: true,
            closed_at: '2026-03-03T00:00:00Z',
          }),
          current_stage: stageState('done'),
          closed_at: '2026-03-03T00:00:00Z',
        },
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

  it('maps in_review to review stage states client-side', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      ...TASK_LIST_RESPONSE,
      tasks: [
        {
          ...SAMPLE_TASKS[0],
          id: 'task-review',
          title: 'Needs review',
          state: canonicalState('needs_review'),
          current_stage: stageState('needs_review'),
        },
        {
          ...SAMPLE_TASKS[1],
          id: 'task-approved',
          title: 'Approved task',
          state: canonicalState('review_approved'),
          current_stage: stageState('review_approved'),
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

  it('test_stages_populated', async () => {
    const stagedTask = {
      ...SAMPLE_TASKS[0],
      state: {
        current_stage: { name: 'development', state: 'needs_review' },
      },
      current_stage: { name: 'development', state: 'needs_review' },
      stages: [
        {
          stage_name: 'development',
          position: 20,
          state: 'needs_review',
          review_policy: 'required',
          updated_at: '2026-05-02T00:00:00Z',
        },
      ],
    }
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      ...TASK_LIST_RESPONSE,
      tasks: [stagedTask],
      total: 1,
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(String(mockFetch.fn.mock.calls[0]?.[0])).toContain('include_stages=1')
    expect(result.current.tasks[0]?.stages[0]).toMatchObject({
      name: 'development',
      display_name: 'Development',
      category: '',
      state: 'needs_review',
      review_policy: 'required',
      position: 20,
      updated_at: '2026-05-02T00:00:00Z',
    })
    expect(result.current.tasks[0]?.current_stage).toMatchObject({
      name: 'development',
      display_name: 'Development',
      state: 'needs_review',
    })
    expect(result.current.tasks[0]?.current_stage?.display_name).not.toBeUndefined()
  })

  it('test_advance_stage_with_action_param', async () => {
    const stageRequests: Array<{ url: string; init?: RequestInit }> = []
    installFetchSpy(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/tasks?')) return jsonResponse(TASK_LIST_RESPONSE)
      if (url.includes('/api/tasks/task-1/stages/build')) {
        stageRequests.push({ url, init })
        return jsonResponse(SAMPLE_TASKS[0])
      }
      return new Response(JSON.stringify({ error: 'unexpected url' }), { status: 404 })
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await (result.current as Phase6TasksApi).advanceStage('task-1', 'build', 'complete')
    })

    const stageRequest = stageRequests[0]
    expect(stageRequest?.url).toContain('/api/tasks/task-1/stages/build')
    expect(stageRequest?.init?.method).toBe('PATCH')
    expect(JSON.parse(String(stageRequest?.init?.body))).toEqual({ action: 'complete' })
  })

  it('test_advance_stage_imports_stage_advance_action_from_shared_helper', () => {
    const source = readFileSync(useTasksSourcePath, 'utf8')

    expect(source).toMatch(
      /import\s+type\s*{[^}]*StageAdvanceAction[^}]*}\s+from\s+['"]\.\.\/lib\/stageActions['"]/,
    )
    expect(source).not.toMatch(/type\s+StageAdvanceAction\s*=/)
  })

  it('test_reject_review_stage_action_sends_reason_payload', async () => {
    const stageRequests: Array<{ url: string; init?: RequestInit }> = []
    installFetchSpy(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/tasks?')) return jsonResponse(TASK_LIST_RESPONSE)
      if (url.includes('/api/tasks/task-1/stages/build')) {
        stageRequests.push({ url, init })
        return jsonResponse(SAMPLE_TASKS[0])
      }
      return new Response(JSON.stringify({ error: 'unexpected url' }), { status: 404 })
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await (result.current as Phase6TasksApi).advanceStage(
        'task-1',
        'build',
        'reject_review',
        'needs tests',
      )
    })

    expect(JSON.parse(String(stageRequests[0]?.init?.body))).toEqual({
      action: 'reject_review',
      reason: 'needs tests',
      notes: 'needs tests',
    })
  })

  it('test_advance_stage_422_propagates_typed_error', async () => {
    const payload = {
      error: 'illegal_stage_transition',
      stage_name: 'build',
      current_state: 'done',
      attempted_transition: 'complete',
      review_policy: 'required',
      reason: 'Done rows cannot advance',
    }
    installFetchSpy(async (input) => {
      const url = String(input)
      if (url.includes('/api/tasks?')) return jsonResponse(TASK_LIST_RESPONSE)
      if (url.includes('/api/tasks/task-1/stages/build')) {
        return new Response(JSON.stringify(payload), {
          status: 422,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ error: 'unexpected url' }), { status: 404 })
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await expect(
      (result.current as Phase6TasksApi).advanceStage('task-1', 'build', 'complete'),
    ).rejects.toMatchObject(payload)
  })

  it('test_fail_stage_mutator', async () => {
    const stageRequests: Array<{ url: string; init?: RequestInit }> = []
    installFetchSpy(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/tasks?')) return jsonResponse(TASK_LIST_RESPONSE)
      if (url.includes('/api/tasks/task-1/stages/build')) {
        stageRequests.push({ url, init })
        return jsonResponse(SAMPLE_TASKS[0])
      }
      return new Response(JSON.stringify({ error: 'unexpected url' }), { status: 404 })
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await (result.current as Phase6TasksApi).failStage(
        'task-1',
        'build',
        'Unit tests failed',
      )
    })

    const stageRequest = stageRequests[0]
    expect(stageRequest?.url).toContain('/api/tasks/task-1/stages/build')
    expect(stageRequest?.init?.method).toBe('PATCH')
    expect(JSON.parse(String(stageRequest?.init?.body))).toEqual({
      action: 'fail',
      reason: 'Unit tests failed',
    })
  })

  it('test_start_stage_mutator', async () => {
    const stageRequests: Array<{ url: string; init?: RequestInit }> = []
    installFetchSpy(async (input, init) => {
      const url = String(input)
      if (url.includes('/api/tasks?')) return jsonResponse(TASK_LIST_RESPONSE)
      if (url.includes('/api/tasks/task-1/stages/build')) {
        stageRequests.push({ url, init })
        return jsonResponse(SAMPLE_TASKS[0])
      }
      return new Response(JSON.stringify({ error: 'unexpected url' }), { status: 404 })
    })

    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await (result.current as Phase6TasksApi).startStage('task-1', 'build')
    })

    const stageRequest = stageRequests[0]
    expect(stageRequest?.url).toContain('/api/tasks/task-1/stages/build')
    expect(stageRequest?.init?.method).toBe('PATCH')
    expect(JSON.parse(String(stageRequest?.init?.body))).toEqual({ action: 'start' })
  })

  it('test_ws_stage_changed_refetches', async () => {
    const { result } = renderHook(() => useTasks())
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    mockFetch.fn.mockClear()

    const stageChangedHandler = mockUseWebSocketEvent.mock.calls.find(
      ([eventType]) => eventType === 'stage_changed',
    )?.[1]

    expect(stageChangedHandler).toBeDefined()

    act(() => {
      stageChangedHandler?.({
        task_id: 'task-1',
        stage_name: 'build',
        state: 'in_progress',
      })
    })

    await waitFor(() => expect(mockFetch.fn).toHaveBeenCalledTimes(1))
  })
})
