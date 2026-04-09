import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'
import { createMockWebSocket, type MockWebSocketInstance } from '../../test/mocks/websocket'

let useSessionDetail: typeof import('../useSessionDetail').useSessionDetail
let mockFetch: MockFetchInstance
let mockWs: {
  instances: MockWebSocketInstance[]
  MockWebSocket: typeof WebSocket
  restore: () => void
}

async function loadModule() {
  vi.resetModules()
  const mod = await import('../useSessionDetail')
  useSessionDetail = mod.useSessionDetail
}

describe('useSessionDetail', () => {
  beforeEach(() => {
    mockFetch = createMockFetch()
    mockWs = createMockWebSocket()
  })

  afterEach(() => {
    mockFetch.restore()
    mockWs.restore()
    vi.restoreAllMocks()
  })

  it('loads parked web-chat history from the chat messages endpoint', async () => {
    await loadModule()
    mockFetch.mockJsonResponse('/api/sessions/sess-web', {
      session: {
        id: 'sess-web',
        external_id: 'chat-ext-1',
        session_type: 'web_chat',
        status: 'paused',
      },
    })
    mockFetch.mockJsonResponse('/api/chat/chat-ext-1/messages', {
      messages: [
        {
          id: 'chat-msg-1',
          role: 'assistant',
          content: 'Recovered after reboot',
          created_at: '2026-04-09T00:00:00Z',
        },
      ],
    })

    const { result } = renderHook(() => useSessionDetail('sess-web'))
    act(() => mockWs.instances[0]?.simulateOpen())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].content).toBe('Recovered after reboot')
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes('/api/chat/chat-ext-1/messages'),
      ),
    ).toBe(true)
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes('/api/sessions/sess-web/messages'),
      ),
    ).toBe(false)
  })

  it('upserts rendered session_message websocket events by message id', async () => {
    await loadModule()
    mockFetch.mockJsonResponse('/api/sessions/sess-cli', {
      session: {
        id: 'sess-cli',
        external_id: 'cli-ext-1',
        session_type: 'terminal',
        status: 'active',
      },
    })
    mockFetch.mockJsonResponse('/api/sessions/sess-cli/messages?limit=10000&offset=0', {
      messages: [
        {
          id: 'sess-msg-1',
          role: 'assistant',
          content: 'Initial output',
          timestamp: '2026-04-09T00:00:00Z',
          content_blocks: [{ type: 'text', content: 'Initial output' }],
        },
      ],
      total_count: 1,
    })

    const { result } = renderHook(() => useSessionDetail('sess-cli'))
    const ws = mockWs.instances[0]
    act(() => ws.simulateOpen())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      ws.simulateMessage({
        type: 'session_message',
        session_id: 'sess-cli',
        message: {
          id: 'sess-msg-1',
          role: 'assistant',
          content: 'Updated output',
          timestamp: '2026-04-09T00:00:01Z',
          content_blocks: [{ type: 'text', content: 'Updated output' }],
        },
      })
    })

    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].content).toBe('Updated output')
    expect(result.current.totalMessages).toBe(1)
  })
})
