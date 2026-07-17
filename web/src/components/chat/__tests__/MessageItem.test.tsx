import { describe, it, expect, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { MessageItem } from '../MessageItem'
import { UnknownBlockCard } from '../UnknownBlockCard'
import type { ChatMessage, ContentBlock } from '../../../types/chat'

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

const contentBlockDispatchCases = {
  text: { type: 'text', content: 'Text block' },
  thinking: { type: 'thinking', content: 'Thinking block' },
  compaction_summary: { type: 'compaction_summary', content: 'Conversation compacted (manual)' },
  tool_chain: {
    type: 'tool_chain',
    tool_calls: [
      {
        id: 'tc-1',
        tool_name: 'read',
        server_name: 'builtin',
        tool_type: 'read',
        status: 'completed',
      },
    ],
  },
  tool_reference: {
    type: 'tool_reference',
    tool_name: 'search',
    server_name: 'builtin',
  },
  attachment: {
    type: 'attachment',
    attachment: {
      id: 'att-doc',
      project_id: 'proj-1',
      filename: 'notes.txt',
      mime_type: 'text/plain',
      size_bytes: 12,
      content_url: '/api/chat/attachments/att-doc/content',
    },
  },
  image: {
    type: 'image',
    image_url: { url: 'https://example.test/generated.png' },
  },
  document: {
    type: 'document',
    source: { name: 'Design notes' },
  },
  web_search_result: {
    type: 'web_search_result',
    content: { title: 'Search hit' },
  },
  resource_link: {
    type: 'resource_link',
    uri: 'file:///src/app.py',
    name: 'src/app.py',
    description: 'Agent referenced source',
  },
  resource: {
    type: 'resource',
    resource: { name: 'Resource doc', text: 'resource body' },
  },
  audio: {
    type: 'audio',
    url: 'data:audio/wav;base64,AAAA',
    mime_type: 'audio/wav',
  },
  diff: {
    type: 'diff',
    path: 'src/main.ts',
    old_text: 'old',
    new_text: 'new',
  },
  terminal: {
    type: 'terminal',
    terminal_id: 'term-1',
  },
  unknown: {
    type: 'unknown',
    block_type: 'custom_payload',
    raw: { custom: true },
  },
} satisfies Record<ContentBlock['type'], ContentBlock>

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
    expect(screen.getByTestId('tool-calls')).toBeTruthy()
  })

  it('renders every content block variant through chat dispatch', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: Object.values(contentBlockDispatchCases),
        })}
      />,
    )

    expect(screen.getByText('Text block')).toBeTruthy()
    expect(screen.getByTestId('thinking')).toHaveTextContent('Thinking block')
    expect(screen.getByText('Conversation compacted (manual)')).toBeTruthy()
    expect(screen.getByText('1 tool calls')).toBeTruthy()
    expect(screen.getByText('Referencing tool: search (builtin)')).toBeTruthy()
    expect(screen.getByText('notes.txt')).toBeTruthy()
    expect(screen.getByAltText('Image content')).toBeTruthy()
    expect(screen.getByText('Design notes')).toBeTruthy()
    expect(screen.getByText('Search result included.')).toBeTruthy()
    expect(screen.getByText('src/app.py')).toBeTruthy()
    expect(screen.getByText('Resource doc')).toBeTruthy()
    expect(screen.getByLabelText('Audio content')).toBeTruthy()
    expect(screen.getByText('src/main.ts')).toBeTruthy()
    expect(screen.getByText('Terminal term-1')).toBeTruthy()
    expect(screen.getByText('custom_payload')).toBeTruthy()
  })

  it('renders UnknownBlockCard for unknown blocks', () => {
    render(<UnknownBlockCard blockType="future_payload" raw={{ value: 42 }} />)

    expect(screen.getByText('Unknown block:')).toBeTruthy()
    expect(screen.getByText('future_payload')).toBeTruthy()
  })

  it('suppresses known protocol metadata while preserving unknown payloads', () => {
    const blocks = [
      { type: 'unknown', block_type: 'turn_completed', raw: { stop_reason: 'end_turn' } },
      { type: 'unknown', block_type: 'retry_state', raw: { attempt: 1 } },
      { type: 'unknown', block_type: 'ui_telemetry', raw: { event: 'prompt_submitted' } },
      { type: 'unknown', block_type: 'file_history_snapshot', raw: { files: [] } },
      { type: 'unknown', block_type: 'custom_payload', raw: { future: true } },
    ] satisfies ContentBlock[]

    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: blocks,
        })}
      />,
    )

    expect(screen.queryByText('turn_completed')).toBeNull()
    expect(screen.queryByText('retry_state')).toBeNull()
    expect(screen.queryByText('ui_telemetry')).toBeNull()
    expect(screen.queryByText('file_history_snapshot')).toBeNull()
    expect(screen.getByText('custom_payload')).toBeTruthy()
    expect(screen.getAllByText('Unknown block:')).toHaveLength(1)
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
    expect(screen.getByTestId('tool-calls')).toBeTruthy()
    expect(screen.getByText('1 tool calls')).toBeTruthy()
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

  it('renders ACP resource link blocks', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'resource_link',
              uri: 'file:///src/app.py',
              name: 'src/app.py',
              description: 'Agent referenced source',
            },
          ],
        })}
      />,
    )

    expect(screen.getByText('src/app.py')).toBeTruthy()
    expect(screen.getByText('Agent referenced source')).toBeTruthy()
    expect(screen.getByText('file:///src/app.py')).toBeTruthy()
  })

  it('renders stored image attachments inline', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'attachment',
              attachment: {
                id: 'att-img',
                project_id: 'proj-1',
                filename: 'screen.png',
                mime_type: 'image/png',
                size_bytes: 12,
                content_url: '/api/chat/attachments/att-img/content',
              },
            },
          ],
        })}
      />,
    )

    const img = screen.getByAltText('screen.png')
    expect(img.getAttribute('src')).toBe('/api/chat/attachments/att-img/content')
  })

  it('renders a filename-aware fallback when a stored image attachment fails to load', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'attachment',
              attachment: {
                id: 'att-img',
                project_id: 'proj-1',
                filename: 'screen.png',
                mime_type: 'image/png',
                size_bytes: 12,
                content_url: '/api/chat/attachments/att-img/content',
              },
            },
          ],
        })}
      />,
    )

    fireEvent.error(screen.getByAltText('screen.png'))

    expect(screen.getByText('screen.png')).toBeTruthy()
    expect(screen.getByText('Image preview unavailable')).toBeTruthy()
    expect(screen.getByText('Download')).toBeTruthy()
  })

  it('renders stored PDF attachments in an embedded viewer', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'attachment',
              attachment: {
                id: 'att-pdf',
                project_id: 'proj-1',
                filename: 'plan.pdf',
                mime_type: 'application/pdf',
                size_bytes: 2048,
                content_url: '/api/chat/attachments/att-pdf/content',
              },
            },
          ],
        })}
      />,
    )

    const iframe = screen.getByTitle('plan.pdf')
    expect(iframe).toHaveAttribute('sandbox', '')
    expect(screen.getByText('Open')).toHaveAttribute('rel', 'noreferrer noopener')
  })

  it('renders an unavailable card for unsafe attachment links', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'attachment',
              attachment: {
                id: 'att-bad',
                project_id: 'proj-1',
                filename: 'bad.txt',
                mime_type: 'text/plain',
                size_bytes: 12,
                content_url: 'javascript:alert(1)',
              },
            },
          ],
        })}
      />,
    )

    expect(screen.getByText('bad.txt')).toBeTruthy()
    expect(screen.getByText('Attachment link unavailable')).toBeTruthy()
    expect(screen.queryByText('Download')).toBeNull()
  })

  it('renders stored document attachments as file cards', () => {
    render(
      <MessageItem
        message={makeMessage({
          content: '',
          contentBlocks: [
            {
              type: 'attachment',
              attachment: {
                id: 'att-doc',
                project_id: 'proj-1',
                filename: 'notes.txt',
                mime_type: 'text/plain',
                size_bytes: 12,
                content_url: '/api/chat/attachments/att-doc/content',
              },
            },
          ],
        })}
      />,
    )

    expect(screen.getByText('notes.txt')).toBeTruthy()
    expect(screen.getByText('Download')).toBeTruthy()
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
