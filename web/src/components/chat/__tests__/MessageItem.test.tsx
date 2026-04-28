import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MessageItem } from '../MessageItem'
import type { ChatMessage } from '../../../types/chat'

// Mock heavy deps
vi.mock('../Markdown', () => ({
  Markdown: ({ content }: { content: string }) => <div data-testid="markdown">{content}</div>,
}))
vi.mock('../ThinkingBlock', () => ({
  ThinkingBlock: ({ content }: { content: string }) => <div data-testid="thinking">{content}</div>,
}))
vi.mock('../ToolCallCard', () => ({
  ToolCallCards: ({ toolCalls }: { toolCalls: unknown[] }) => (
    <div data-testid="tool-calls">{toolCalls.length} tool calls</div>
  ),
  ToolChainGroup: ({ toolCalls }: { toolCalls: unknown[] }) => (
    <div data-testid="tool-chain">{toolCalls.length} tools</div>
  ),
}))

function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'msg-1',
    role: 'assistant',
    content: 'Hello world',
    timestamp: new Date('2026-03-01T12:00:00Z'),
    ...overrides,
  }
}

describe('MessageItem', () => {
  it('renders user message with "You" label', () => {
    render(<MessageItem message={makeMessage({ role: 'user', content: 'Hi there' })} />)

    expect(screen.getByText('You')).toBeTruthy()
    expect(screen.getByText('Hi there')).toBeTruthy()
  })

  it('renders assistant message with "Gobby" label', () => {
    render(<MessageItem message={makeMessage()} />)

    expect(screen.getByText('Gobby')).toBeTruthy()
    expect(screen.getByTestId('markdown')).toBeTruthy()
  })

  it('renders system message with "System" label', () => {
    render(
      <MessageItem message={makeMessage({ role: 'system', content: 'System notice' })} />,
    )

    expect(screen.getByText('System')).toBeTruthy()
  })

  it('shows thinking indicator when isThinking and no content', () => {
    render(
      <MessageItem
        message={makeMessage({ content: '', thinkingContent: undefined })}
        isThinking={true}
      />,
    )

    expect(screen.getByText('Thinking...')).toBeTruthy()
  })

  it('renders thinking block when thinkingContent exists', () => {
    render(
      <MessageItem message={makeMessage({ thinkingContent: 'Let me think...' })} />,
    )

    expect(screen.getByTestId('thinking')).toBeTruthy()
    expect(screen.getByText('Let me think...')).toBeTruthy()
  })

  it('renders tool calls via ToolCallCards', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          toolCalls: [
            { id: 'tc-1', tool_name: 'read_file', server_name: 'builtin', tool_type: 'read', status: 'completed' },
          ],
        })}
      />,
    )

    expect(screen.getByTestId('tool-calls')).toBeTruthy()
    expect(screen.getByText('1 tool calls')).toBeTruthy()
  })

  it('renders content blocks with interleaved text and tools', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            { type: 'text', content: 'First text' },
            {
              type: 'tool_chain',
              tool_calls: [
                { id: 'tc-1', tool_name: 'read', server_name: 'b', tool_type: 'read', status: 'completed' },
              ],
            },
            { type: 'text', content: 'Second text' },
          ],
        })}
      />,
    )

    const markdowns = screen.getAllByTestId('markdown')
    expect(markdowns).toHaveLength(2)
    expect(screen.getByTestId('tool-chain')).toBeTruthy()
  })

  it('renders protocol tags inside text as collapsed tool chains', () => {
    render(
      <MessageItem
        message={makeMessage({
          content:
            'Visible text\n<environment_context><shell>zsh</shell></environment_context>\nTrailing text',
        })}
      />,
    )

    const markdowns = screen.getAllByTestId('markdown')
    expect(markdowns).toHaveLength(2)
    expect(markdowns[0].textContent).toContain('Visible text')
    expect(markdowns[1].textContent).toContain('Trailing text')
    expect(screen.getByTestId('tool-chain')).toBeTruthy()
    expect(screen.getByText('1 tools')).toBeTruthy()
  })

  it('renders base64 image blocks', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'image',
              source: { type: 'base64', media_type: 'image/png', data: 'abc' },
            },
          ],
        })}
      />,
    )

    const img = screen.getByAltText('Image content')
    expect(img).toBeTruthy()
    expect(img.getAttribute('src')).toBe('data:image/png;base64,abc')
  })

  it('renders image blocks with image_url sources', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'image',
              image_url: { url: 'https://example.test/generated.png' },
            },
          ],
        })}
      />,
    )

    const img = screen.getByAltText('Image content')
    expect(img).toBeTruthy()
    expect(img.getAttribute('src')).toBe('https://example.test/generated.png')
  })

  it('shows streaming cursor when isStreaming', () => {
    const { container } = render(
      <MessageItem message={makeMessage()} isStreaming={true} />,
    )

    expect(container.querySelector('.cursor')).toBeTruthy()
  })

  it('only shows one streaming cursor on the final text content block', () => {
    const { container } = render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            { type: 'text', content: 'First text' },
            {
              type: 'tool_chain',
              tool_calls: [
                { id: 'tc-1', tool_name: 'read', server_name: 'b', tool_type: 'read', status: 'completed' },
              ],
            },
            { type: 'text', content: 'Second text' },
          ],
        })}
        isStreaming={true}
      />,
    )

    expect(container.querySelectorAll('.cursor')).toHaveLength(1)
  })

  it('returns null for empty messages', () => {
    const { container } = render(
      <MessageItem
        message={makeMessage({ content: '', thinkingContent: undefined, toolCalls: undefined, contentBlocks: undefined })}
      />,
    )

    expect(container.innerHTML).toBe('')
  })

  it('renders model switch messages as centered pill', () => {
    render(
      <MessageItem
        message={makeMessage({
          id: 'model-switch-1',
          role: 'system',
          content: 'Switched to claude-4',
        })}
      />,
    )

    expect(screen.getByText('Switched to claude-4')).toBeTruthy()
  })

  it('shows timestamp', () => {
    render(<MessageItem message={makeMessage()} />)

    // Should show localized time string
    const timeEl = screen.getByText(/\d{1,2}:\d{2}/)
    expect(timeEl).toBeTruthy()
  })
})
