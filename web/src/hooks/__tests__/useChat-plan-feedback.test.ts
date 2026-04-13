import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createMockWebSocket, type MockWebSocketInstance } from '../../test/mocks/websocket'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

let mockWs: { instances: MockWebSocketInstance[]; MockWebSocket: typeof WebSocket; restore: () => void }
let mockFetch: MockFetchInstance
let useChat: typeof import('../useChat').useChat
let originalLocalStorage: Storage

beforeEach(() => {
  mockWs = createMockWebSocket()
  mockFetch = createMockFetch()
  // Mock localStorage — seed a conversation id so useChat initializes with
  // one on mount. Matches real-world flow: by the time plan_pending_approval
  // arrives, a session has already been bound via ensureMainSession.
  originalLocalStorage = globalThis.localStorage
  const store: Record<string, string> = {
    'gobby-conversation-id': 'test-conversation-id',
    'gobby-db-session-id': 'test-conversation-id',
  }
  const mockStorage = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value }),
    removeItem: vi.fn((key: string) => { delete store[key] }),
    clear: vi.fn(() => { Object.keys(store).forEach(k => delete store[k]) }),
    key: vi.fn((_index: number) => null),
    get length() { return Object.keys(store).length },
  }
  Object.defineProperty(globalThis, 'localStorage', { value: mockStorage, writable: true, configurable: true })
  vi.useFakeTimers()
})

afterEach(() => {
  mockWs.restore()
  mockFetch.restore()
  Object.defineProperty(globalThis, 'localStorage', { value: originalLocalStorage, writable: true, configurable: true })
  vi.useRealTimers()
  vi.restoreAllMocks()
})

async function loadModule() {
  vi.resetModules()
  const mod = await import('../useChat')
  useChat = mod.useChat
}

describe('useChat plan feedback', () => {
  it('auto-sends feedback message on plan_changes_requested', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    // 1. Simulate a plan pending approval
    act(() => {
      ws.simulateMessage({
        type: 'plan_pending_approval',
        plan_content: '# My Plan\n\nStep 1...',
      })
    })
    expect(result.current.planPendingApproval).toBe(true)

    // 2. Request changes with feedback
    const feedback = 'Please add more detail to Step 1'
    act(() => {
      result.current.requestPlanChanges(feedback)
    })

    // Should have sent plan_approval_response
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    expect(sentMsg.type).toBe('plan_approval_response')
    expect(sentMsg.decision).toBe('request_changes')
    expect(sentMsg.feedback).toBe(feedback)

    // Approval UI should be cleared immediately
    expect(result.current.planPendingApproval).toBe(false)

    // 3. Simulate backend confirming the change with mode_changed
    act(() => {
      ws.simulateMessage({
        type: 'mode_changed',
        mode: 'plan',
        reason: 'plan_changes_requested',
        conversation_id: result.current.conversationId,
      })
    })

    // 4. Advance timers to trigger auto-send setTimeout
    // Use async act to flush microtasks from sendMessage state updates
    await act(async () => {
      vi.advanceTimersByTime(200)
    })

    // Should have sent the feedback as a chat message
    const lastSent = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    expect(lastSent.type).toBe('chat_message')
    expect(lastSent.content).toBe(feedback)

    // Messages should include the feedback
    const userMsgs = result.current.messages.filter(m => m.role === 'user')
    expect(userMsgs).toHaveLength(1)
    expect(userMsgs[0].content).toBe(feedback)
  })
})
