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
    vi.useRealTimers()
    mockFetch.restore()
    mockWs.restore()
    vi.restoreAllMocks()
  })

  it('falls back to chat messages for parked web chats without transcript-backed history', async () => {
    await loadModule()
    mockFetch.mockJsonResponse('/api/sessions/sess-web', {
      session: {
        id: 'sess-web',
        external_id: 'chat-ext-1',
        session_type: 'web_chat',
        status: 'paused',
      },
    })
    mockFetch.mockJsonResponse('/api/sessions/sess-web/messages?limit=10000&offset=0', {
      messages: [],
      total_count: 0,
    })
    mockFetch.mockJsonResponse('/api/chat/sess-web/messages', {
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
        String(url).includes('/api/chat/sess-web/messages'),
      ),
    ).toBe(true)
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes('/api/sessions/sess-web/messages'),
      ),
    ).toBe(true)
  })

  it('prefers rendered session messages for transcript-backed web chats', async () => {
    await loadModule()
    mockFetch.mockJsonResponse('/api/sessions/sess-gemini/messages?limit=10000&offset=0', {
      messages: [
        {
          id: 'sess-msg-1',
          role: 'assistant',
          content: 'Transcript-backed Gemini response',
          timestamp: '2026-04-09T00:00:00Z',
          content_blocks: [{ type: 'text', content: 'Transcript-backed Gemini response' }],
        },
      ],
      total_count: 1,
    })
    mockFetch.mockJsonResponse(/^\/api\/sessions\/sess-gemini$/, {
      session: {
        id: 'sess-gemini',
        external_id: 'gemini-ext-1',
        session_type: 'web_chat',
        transcript_path: '/tmp/gemini-session.json',
        status: 'paused',
      },
    })

    const { result } = renderHook(() => useSessionDetail('sess-gemini'))
    act(() => mockWs.instances[0]?.simulateOpen())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.messages).toHaveLength(1)
    expect(result.current.messages[0].content).toBe('Transcript-backed Gemini response')
    expect(
      mockFetch.fn.mock.calls.some(([url]) =>
        String(url).includes('/api/chat/gemini-ext-1/messages'),
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

  it('polls chat-backed web chats for live updates', async () => {
    vi.useFakeTimers()
    await loadModule()

    let chatFetchCount = 0
    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url =
        typeof input === 'string'
          ? input
          : input instanceof URL
            ? input.toString()
            : input.url

      if (url.includes('/api/sessions/sess-web/messages?limit=10000&offset=0')) {
        return new Response(
          JSON.stringify({ messages: [], total_count: 0 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }

      if (/\/api\/sessions\/sess-web$/.test(url)) {
        return new Response(
          JSON.stringify({
            session: {
              id: 'sess-web',
              external_id: 'chat-ext-2',
              session_type: 'web_chat',
              status: 'active',
            },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }

      if (url.includes('/api/chat/sess-web/messages')) {
        chatFetchCount += 1
        const content =
          chatFetchCount === 1 ? 'First parked reply' : 'Updated parked reply'
        return new Response(
          JSON.stringify({
            messages: [
              {
                id: `chat-msg-${chatFetchCount}`,
                role: 'assistant',
                content,
                created_at: '2026-04-09T00:00:00Z',
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      }

      return new Response(JSON.stringify({ error: 'no mock route matched' }), {
        status: 404,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    const { result } = renderHook(() => useSessionDetail('sess-web'))
    act(() => mockWs.instances[0]?.simulateOpen())

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(result.current.messages[0].content).toBe('First parked reply')

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(result.current.messages[0].content).toBe('Updated parked reply')
  })
})
