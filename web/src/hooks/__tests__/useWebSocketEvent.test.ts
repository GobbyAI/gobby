import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createMockWebSocket, type MockWebSocketInstance } from '../../test/mocks/websocket'

// The module uses module-level singleton state, so we need to reset it between tests.
// We re-import after resetting modules.
let useWebSocketEvent: typeof import('../useWebSocketEvent').useWebSocketEvent
let mockWs: { instances: MockWebSocketInstance[]; MockWebSocket: typeof WebSocket; restore: () => void }

beforeEach(() => {
  mockWs = createMockWebSocket()
  vi.useFakeTimers()
})

afterEach(() => {
  mockWs.restore()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// Helper: dynamically import the module fresh each test to reset singleton state
async function loadModule() {
  vi.resetModules()
  const mod = await import('../useWebSocketEvent')
  useWebSocketEvent = mod.useWebSocketEvent
}

describe('useWebSocketEvent', () => {
  it('creates a WebSocket connection on mount', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    expect(mockWs.instances).toHaveLength(1)
    expect(mockWs.instances[0].url).toContain('/ws')
  })

  it('sends subscribe message on open', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    expect(ws.send).toHaveBeenCalledWith(
      expect.stringContaining('"type":"subscribe"'),
    )
    const payload = JSON.parse(ws.send.mock.calls[0][0])
    expect(payload.events).toContain('task_event')
  })

  it('dispatches messages to matching handler', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    act(() => ws.simulateMessage({ type: 'task_event', id: '1' }))

    expect(handler).toHaveBeenCalledWith({ type: 'task_event', id: '1' })
  })

  it('does not dispatch messages to non-matching handler', async () => {
    await loadModule()
    const taskHandler = vi.fn()
    const sessionHandler = vi.fn()
    renderHook(() => {
      useWebSocketEvent('task_event', taskHandler)
      useWebSocketEvent('session_event', sessionHandler)
    })

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    act(() => ws.simulateMessage({ type: 'task_event', data: 'hello' }))

    expect(taskHandler).toHaveBeenCalledTimes(1)
    expect(sessionHandler).not.toHaveBeenCalled()
  })

  it('multiple handlers for the same event type all fire', async () => {
    await loadModule()
    const handler1 = vi.fn()
    const handler2 = vi.fn()

    const { unmount: unmount1 } = renderHook(() => useWebSocketEvent('task_event', handler1))
    renderHook(() => useWebSocketEvent('task_event', handler2))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    act(() => ws.simulateMessage({ type: 'task_event', id: '1' }))

    expect(handler1).toHaveBeenCalledTimes(1)
    expect(handler2).toHaveBeenCalledTimes(1)

    // After unmounting one handler, only the other fires
    unmount1()
    act(() => ws.simulateMessage({ type: 'task_event', id: '2' }))

    expect(handler1).toHaveBeenCalledTimes(1)
    expect(handler2).toHaveBeenCalledTimes(2)
  })

  it('closes WebSocket when all handlers unmount', async () => {
    await loadModule()
    const handler = vi.fn()
    const { unmount } = renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    unmount()
    expect(ws.close).toHaveBeenCalled()
  })

  it('reconnects on close with exponential backoff', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    // Simulate close
    act(() => ws.simulateClose())

    // Should not reconnect immediately
    expect(mockWs.instances).toHaveLength(1)

    // Advance timer past the base delay (1000ms + jitter)
    act(() => vi.advanceTimersByTime(2000))

    expect(mockWs.instances).toHaveLength(2)
  })

  it('ignores malformed JSON messages', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    act(() => ws.simulateMessage('not json {{{'))

    expect(handler).not.toHaveBeenCalled()
  })

  it('ignores messages without a type field', async () => {
    await loadModule()
    const handler = vi.fn()
    renderHook(() => useWebSocketEvent('task_event', handler))

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    act(() => ws.simulateMessage({ data: 'no type here' }))

    expect(handler).not.toHaveBeenCalled()
  })

  it('uses latest handler via ref (no stale closure)', async () => {
    await loadModule()
    const handler1 = vi.fn()
    const handler2 = vi.fn()

    const { rerender } = renderHook(
      ({ handler }) => useWebSocketEvent('task_event', handler),
      { initialProps: { handler: handler1 } },
    )

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    // Switch to handler2
    rerender({ handler: handler2 })

    act(() => ws.simulateMessage({ type: 'task_event', id: '1' }))

    expect(handler1).not.toHaveBeenCalled()
    expect(handler2).toHaveBeenCalledTimes(1)
  })

  it('resubscribes when event type changes', async () => {
    await loadModule()
    const handler = vi.fn()

    const { rerender } = renderHook(
      ({ eventType }) => useWebSocketEvent(eventType, handler),
      { initialProps: { eventType: 'task_event' } },
    )

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    // Change event type
    rerender({ eventType: 'session_event' })

    // Old event should not trigger handler
    act(() => ws.simulateMessage({ type: 'task_event', id: '1' }))
    expect(handler).not.toHaveBeenCalled()

    // New event should
    act(() => ws.simulateMessage({ type: 'session_event', id: '2' }))
    expect(handler).toHaveBeenCalledWith({ type: 'session_event', id: '2' })
  })
})
