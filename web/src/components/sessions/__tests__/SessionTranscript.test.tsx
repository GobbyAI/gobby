import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SessionTranscript } from '../SessionTranscript'

vi.mock('../../chat/MessageItem', () => ({
  MessageItem: ({ message }: { message: { role: string; content: string } }) => (
    <div data-testid="message-item">
      {message.role}:{message.content || '<empty>'}
    </div>
  ),
}))

describe('SessionTranscript', () => {
  it('renders protocol-only rendered user messages as system messages', () => {
    render(
      <SessionTranscript
        messages={[
          {
            id: 'sess-msg-1',
            role: 'user',
            content: '',
            timestamp: '2026-04-16T00:00:00Z',
            content_blocks: [
              {
                type: 'tool_chain',
                tool_calls: [
                  {
                    id: 'protocol-1',
                    tool_name: 'protocol_context',
                    server_name: 'builtin',
                    tool_type: 'protocol',
                    status: 'completed',
                  },
                ],
              },
            ],
          },
        ]}
        totalMessages={1}
        isLoading={false}
      />,
    )

    expect(screen.getByText('system:<empty>')).toBeTruthy()
  })

  it('keeps genuine rendered user text as user messages', () => {
    render(
      <SessionTranscript
        messages={[
          {
            id: 'sess-msg-2',
            role: 'user',
            content: 'Actual user question',
            timestamp: '2026-04-16T00:00:00Z',
            content_blocks: [{ type: 'text', content: 'Actual user question' }],
          },
        ]}
        totalMessages={1}
        isLoading={false}
      />,
    )

    expect(screen.getByText('user:Actual user question')).toBeTruthy()
  })
})
