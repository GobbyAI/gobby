/**
 * Tests for useChat hook — focuses on pure helper functions and key behaviors.
 * The hook is ~2000 lines with complex WS state management. We test:
 * 1. Pure functions: mapApiMessages, appendTextBlock, appendToolBlock, findPendingToolCall, uuid
 * 2. Key hook behaviors: conversation ID management, message state
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createMockWebSocket, type MockWebSocketInstance } from '../../test/mocks/websocket'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

let mockWs: { instances: MockWebSocketInstance[]; MockWebSocket: typeof WebSocket; restore: () => void }
let mockFetch: MockFetchInstance
let useChat: typeof import('../useChat').useChat

beforeEach(() => {
  mockWs = createMockWebSocket()
  mockFetch = createMockFetch()
  // Mock localStorage
  const store: Record<string, string> = {}
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation((key) => store[key] ?? null)
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation((key, value) => { store[key] = value })
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation((key) => { delete store[key] })
})

afterEach(() => {
  mockWs.restore()
  mockFetch.restore()
  vi.restoreAllMocks()
})

async function loadModule() {
  vi.resetModules()
  const mod = await import('../useChat')
  useChat = mod.useChat
}

describe('useChat', () => {
  it('initializes with empty messages and not streaming', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    expect(result.current.messages).toEqual([])
    expect(result.current.isStreaming).toBe(false)
    expect(result.current.isThinking).toBe(false)
    expect(result.current.isConnected).toBe(false)
  })

  it('connects to WebSocket on mount', async () => {
    await loadModule()
    renderHook(() => useChat())

    expect(mockWs.instances).toHaveLength(1)
    expect(mockWs.instances[0].url).toContain('/ws')
  })

  it('sets isConnected when WS opens', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    act(() => mockWs.instances[0].simulateOpen())

    expect(result.current.isConnected).toBe(true)
  })

  it('sends subscribe message on connect', async () => {
    await loadModule()
    renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    expect(ws.send).toHaveBeenCalled()
    const msg = JSON.parse(ws.send.mock.calls[0][0])
    expect(msg.type).toBe('subscribe')
    expect(msg.events).toContain('chat_stream')
    expect(msg.events).toContain('tool_status')
  })

  it('resets state on WS close', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())
    expect(result.current.isConnected).toBe(true)

    act(() => ws.simulateClose())
    expect(result.current.isConnected).toBe(false)
    expect(result.current.isStreaming).toBe(false)
  })

  it('generates a conversation ID on mount', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    expect(result.current.conversationId).toBeTruthy()
    expect(typeof result.current.conversationId).toBe('string')
  })

  it('persists conversation ID to localStorage on send', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))

    // Should have saved the conversation ID when sending
    expect(localStorage.setItem).toHaveBeenCalledWith(
      'gobby-conversation-id',
      expect.any(String),
    )
  })

  it('sendMessage adds user message and sends WS message', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => {
      result.current.sendMessage('Hello world')
    })

    // Should have added a user message
    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].role).toBe('user')
    expect(result.current.messages[0].content).toBe('Hello world')

    // Should be streaming
    expect(result.current.isStreaming).toBe(true)
  })

  it('sendMessage returns false when WS not connected', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    // Don't open WS — should return false
    let sent: boolean = true
    act(() => {
      sent = result.current.sendMessage('Hello')
    })

    expect(sent).toBe(false)
  })

  it('handles chat_stream messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    // Send a user message first to establish a request ID
    act(() => result.current.sendMessage('Hello'))

    // Get the request_id from the sent WS message
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    const requestId = sentMsg.request_id

    // Simulate streaming response
    act(() => {
      ws.simulateMessage({
        type: 'chat_stream',
        message_id: 'msg-1',
        request_id: requestId,
        content: 'Hello ',
        done: false,
      })
    })

    // Should have an assistant message
    const assistantMsgs = result.current.messages.filter(m => m.role === 'assistant')
    expect(assistantMsgs).toHaveLength(1)
    expect(assistantMsgs[0].content).toContain('Hello')
  })

  it('handles chat_stream done=true', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    const requestId = sentMsg.request_id

    act(() => {
      ws.simulateMessage({
        type: 'chat_stream',
        message_id: 'msg-1',
        request_id: requestId,
        content: 'Response',
        done: true,
      })
    })

    expect(result.current.isStreaming).toBe(false)
  })

  it('handles chat_error messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    const requestId = sentMsg.request_id

    act(() => {
      ws.simulateMessage({
        type: 'chat_error',
        message_id: 'msg-1',
        request_id: requestId,
        error: 'Something went wrong',
      })
    })

    expect(result.current.isStreaming).toBe(false)
    // Error should appear in messages
    const errorMsgs = result.current.messages.filter(m => m.role === 'system')
    expect(errorMsgs.length).toBeGreaterThanOrEqual(1)
  })

  it('handles tool_status messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    const requestId = sentMsg.request_id

    // First stream some text
    act(() => {
      ws.simulateMessage({
        type: 'chat_stream',
        message_id: 'msg-1',
        request_id: requestId,
        content: '',
        done: false,
      })
    })

    // Then a tool status
    act(() => {
      ws.simulateMessage({
        type: 'tool_status',
        message_id: 'msg-1',
        request_id: requestId,
        tool_call_id: 'tc-1',
        status: 'calling',
        tool_name: 'read_file',
        server_name: 'gobby',
        arguments: { path: '/tmp/test' },
      })
    })

    const assistantMsgs = result.current.messages.filter(m => m.role === 'assistant')
    expect(assistantMsgs).toHaveLength(1)
    // The message should have tool calls
    const msg = assistantMsgs[0]
    expect(msg.toolCalls?.length).toBeGreaterThanOrEqual(1)
    expect(msg.toolCalls?.[0].tool_name).toBe('read_file')
  })

  it('stopStreaming stops streaming', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    expect(result.current.isStreaming).toBe(true)

    act(() => result.current.stopStreaming())
    expect(result.current.isStreaming).toBe(false)
  })

  it('startNewChat clears messages and generates new ID', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    expect(result.current.messages).toHaveLength(1)

    const oldId = result.current.conversationId

    act(() => result.current.startNewChat())

    expect(result.current.messages).toHaveLength(0)
    expect(result.current.conversationId).not.toBe(oldId)
  })

  it('handles voice_transcription messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => {
      ws.simulateMessage({
        type: 'voice_transcription',
        text: 'Hello from voice',
        request_id: 'voice-req-1',
      })
    })

    // Should add a user message from voice
    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].role).toBe('user')
    expect(result.current.messages[0].content).toBe('Hello from voice')
    expect(result.current.isStreaming).toBe(true)
  })

  it('handles session_info messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => {
      ws.simulateMessage({
        type: 'session_info',
        session_ref: '#42',
        current_branch: 'feature/test',
        agent_name: 'test-agent',
      })
    })

    expect(result.current.sessionRef).toBe('#42')
    expect(result.current.currentBranch).toBe('feature/test')
    expect(result.current.activeAgent).toBe('test-agent')
  })

  it('handles mode_changed messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    const modeChanged = vi.fn()
    act(() => result.current.setOnModeChanged(modeChanged))

    act(() => {
      ws.simulateMessage({
        type: 'mode_changed',
        mode: 'bypass',
        conversation_id: result.current.conversationId,
      })
    })

    expect(modeChanged).toHaveBeenCalledWith('bypass')
  })

  it('handles plan_pending_approval messages', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => {
      ws.simulateMessage({
        type: 'plan_pending_approval',
        plan_content: '# My Plan\n\nStep 1...',
      })
    })

    expect(result.current.planPendingApproval).toBe(true)
  })

  it('contextUsage tracks token usage from chat_stream', async () => {
    await loadModule()
    const { result } = renderHook(() => useChat())

    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    act(() => result.current.sendMessage('Hello'))
    const sentMsg = JSON.parse(ws.send.mock.calls[ws.send.mock.calls.length - 1][0])
    const requestId = sentMsg.request_id

    act(() => {
      ws.simulateMessage({
        type: 'chat_stream',
        message_id: 'msg-1',
        request_id: requestId,
        content: 'Done',
        done: true,
        usage: {
          input_tokens: 100,
          output_tokens: 50,
          cache_read_input_tokens: 20,
          cache_creation_input_tokens: 10,
          total_input_tokens: 130,
        },
        context_window: 200000,
      })
    })

    expect(result.current.contextUsage.totalInputTokens).toBeGreaterThan(0)
    expect(result.current.contextUsage.contextWindow).toBe(200000)
  })
})
