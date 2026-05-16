import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'
import { usePipelineExecutions } from '../usePipelineExecutions'

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: () => undefined,
}))

let mockFetch: MockFetchInstance

const SAMPLE_RESPONSE = {
  executions: [
    {
      id: 'pe-1',
      pipeline_name: 'deploy',
      project_id: 'proj-1',
      status: 'completed',
      created_at: '2026-04-01T00:00:00Z',
      updated_at: '2026-04-01T00:01:00Z',
      completed_at: '2026-04-01T00:01:00Z',
      inputs_json: null,
      outputs_json: null,
      definition_json: null,
      parent_execution_id: null,
      steps: [],
    },
  ],
  total: 42,
  limit: 10,
  offset: 0,
  status_summary: { completed: 30, failed: 12 },
}

beforeEach(() => {
  mockFetch = createMockFetch()
  mockFetch.mockJsonResponse('/api/pipelines/executions', SAMPLE_RESPONSE)
})

afterEach(() => {
  mockFetch.restore()
  vi.restoreAllMocks()
})

describe('usePipelineExecutions', () => {
  it('exposes total, limit, offset, and statusSummary from the response', async () => {
    const { result } = renderHook(() =>
      usePipelineExecutions({ projectId: 'proj-1', limit: 10, offset: 0 }),
    )

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })

    expect(result.current.executions).toHaveLength(1)
    expect(result.current.total).toBe(42)
    expect(result.current.limit).toBe(10)
    expect(result.current.offset).toBe(0)
    expect(result.current.statusSummary).toEqual({ completed: 30, failed: 12 })
    expect(result.current.error).toBeNull()
  })

  it('sends limit and offset as query params', async () => {
    renderHook(() =>
      usePipelineExecutions({ projectId: 'proj-1', limit: 25, offset: 50 }),
    )

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalled()
    })

    const url = String(mockFetch.fn.mock.calls[0][0])
    expect(url).toContain('limit=25')
    expect(url).toContain('offset=50')
    expect(url).toContain('project_id=proj-1')
  })

  it('refetches when offset changes', async () => {
    const { rerender } = renderHook(
      ({ offset }: { offset: number }) =>
        usePipelineExecutions({ projectId: 'proj-1', limit: 10, offset }),
      { initialProps: { offset: 0 } },
    )

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalledTimes(1)
    })

    rerender({ offset: 10 })

    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalledTimes(2)
    })
    const secondUrl = String(mockFetch.fn.mock.calls[1][0])
    expect(secondUrl).toContain('offset=10')
  })

  it('accepts a bare projectId string for backward compatibility', async () => {
    const { result } = renderHook(() => usePipelineExecutions('proj-1'))

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
    expect(result.current.total).toBe(42)
    expect(result.current.limit).toBe(50)
    expect(result.current.offset).toBe(0)
  })

  it('reports error and zeroes total when the request fails', async () => {
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => undefined)
    mockFetch.resetRoutes()
    mockFetch.mockErrorResponse('/api/pipelines/executions', 500, 'boom')

    const { result } = renderHook(() =>
      usePipelineExecutions({ projectId: 'proj-1' }),
    )

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
    expect(result.current.total).toBe(0)
    expect(result.current.executions).toEqual([])
    expect(result.current.error).toContain('500')
    expect(consoleError).toHaveBeenCalledWith(
      'Failed to fetch pipeline executions:',
      500,
      'Error 500',
    )
  })
})
