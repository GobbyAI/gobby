import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useTraceDetail, useTraces } from '../useTraces'

let websocketHandler: ((data: { trace_id?: string }) => void) | undefined

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: (_eventType: string, handler: (data: { trace_id?: string }) => void) => {
    websocketHandler = handler
  },
}))

interface DeferredResponse {
  promise: Promise<Response>
  resolve: (response: Response) => void
}

function deferredResponse(): DeferredResponse {
  let resolve!: (response: Response) => void
  const promise = new Promise<Response>((resolvePromise) => {
    resolve = resolvePromise
  })
  return { promise, resolve }
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

describe('useTraces request lifecycle', () => {
  beforeEach(() => {
    websocketHandler = undefined
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('ignores a stale detail response after switching traces', async () => {
    const traceA = deferredResponse()
    const traceB = deferredResponse()
    vi.stubGlobal('fetch', vi.fn((url: string) => (
      url.endsWith('/trace-a') ? traceA.promise : traceB.promise
    )))

    const { result, rerender } = renderHook(
      ({ traceId }) => useTraceDetail(traceId),
      { initialProps: { traceId: 'trace-a' } },
    )

    rerender({ traceId: 'trace-b' })
    await act(async () => {
      traceB.resolve(jsonResponse({ spans: [{ id: 'span-b' }] }))
    })
    await act(async () => {
      traceA.resolve(jsonResponse({ spans: [{ id: 'span-a' }] }))
    })

    expect(result.current.spans).toEqual([{ id: 'span-b' }])
    expect(result.current.isLoading).toBe(false)
  })

  it('clears a pending detail debounce when the trace changes', async () => {
    const fetchMock = vi.fn(async (_url: string) => jsonResponse({ spans: [] }))
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderHook(
      ({ traceId }) => useTraceDetail(traceId),
      { initialProps: { traceId: 'trace-a' } },
    )
    await act(async () => undefined)

    act(() => websocketHandler?.({ trace_id: 'trace-a' }))
    rerender({ traceId: 'trace-b' })
    await act(async () => undefined)
    act(() => vi.advanceTimersByTime(500))

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      '/api/traces/trace-a',
      '/api/traces/trace-b',
    ])
  })

  it('clears a pending list debounce when its query changes', async () => {
    const fetchMock = vi.fn(async (_url: string) => jsonResponse({ traces: [] }))
    vi.stubGlobal('fetch', fetchMock)

    const { rerender } = renderHook(
      ({ projectId }) => useTraces(projectId),
      { initialProps: { projectId: 'project-a' } },
    )
    await act(async () => undefined)

    act(() => websocketHandler?.({}))
    rerender({ projectId: 'project-b' })
    await act(async () => undefined)
    act(() => vi.advanceTimersByTime(500))

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps loaded traces and exposes an error when refetch fails', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ traces: [{ trace_id: 'trace-1' }] }))
      .mockResolvedValueOnce({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
      } as Response)
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useTraces('project-123'))
    await act(async () => undefined)

    await act(async () => {
      await result.current.fetchTraces()
    })

    expect(result.current.traces).toEqual([{ trace_id: 'trace-1' }])
    expect(result.current.error).toBe('Failed to fetch traces (500)')
  })
})
