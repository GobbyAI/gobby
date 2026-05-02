import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SessionViewer } from '../SessionViewer'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('SessionViewer', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('counts hidden transcript messages after filtering empty previews', async () => {
    const messages = [
      ...Array.from({ length: 9 }, (_, i) => ({
        role: 'assistant',
        content: `visible message ${i}`,
        tool_name: null,
        timestamp: `2026-05-02T00:00:0${i}Z`,
      })),
      {
        role: 'assistant',
        content: null,
        tool_name: null,
        timestamp: '2026-05-02T00:01:00Z',
      },
      {
        role: 'tool',
        content: '',
        tool_name: null,
        timestamp: '2026-05-02T00:01:01Z',
      },
    ]
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith('/messages?limit=10')) {
        return Promise.resolve(jsonResponse({ messages }))
      }
      return Promise.resolve(
        jsonResponse({
          session: {
            id: 'session-1',
            ref: '#42',
            source: 'claude',
            status: 'closed',
            title: 'Finished task',
            message_count: messages.length,
            created_at: '2026-05-02T00:00:00Z',
            updated_at: '2026-05-02T00:10:00Z',
            model: 'claude-test',
          },
        }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)

    render(<SessionViewer sessionId="session-1" />)

    await waitFor(() => {
      expect(screen.getByText('Finished task')).toBeInTheDocument()
    })
    await userEvent.click(screen.getByRole('button', { name: /show transcript preview/i }))

    expect(screen.getByText('visible message 0')).toBeInTheDocument()
    expect(screen.queryByText('visible message 8')).toBeNull()
    expect(screen.getByText('+ 1 more messages')).toBeInTheDocument()
  })
})
