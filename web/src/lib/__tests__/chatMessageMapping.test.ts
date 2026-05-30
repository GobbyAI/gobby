import { describe, expect, it } from 'vitest'

import {
  findPendingToolCall,
  looksLikeSystemBootstrapText,
  mapApiMessages,
  mapRenderedMessageToChatMessage,
  normalizeChatRole,
} from '../chatMessageMapping'

describe('chatMessageMapping', () => {
  it('detects Codex bootstrap text as system instructions', () => {
    const content = `AGENTS.md instructions for /Users/josh/Projects/gobby

# Personality
You are a deeply pragmatic engineer.

## Interaction Style
Stay concise and direct.`

    expect(looksLikeSystemBootstrapText(content)).toBe(true)
    expect(normalizeChatRole('user', content)).toBe('system')
  })

  it('does not reclassify ordinary user text', () => {
    expect(normalizeChatRole('user', 'Please read AGENTS.md and summarize it.')).toBe('user')
  })

  it('does not reclassify ordinary heading-heavy markdown', () => {
    const content = [
      '# Release Notes',
      '',
      '## Platform Context',
      'Product-facing notes.',
      '',
      '## Capabilities',
      'Filtering and sorting.',
      '',
      '## Interaction Style',
      'Compact controls.',
    ].join('\n')

    expect(looksLikeSystemBootstrapText(content)).toBe(false)
    expect(normalizeChatRole('user', content)).toBe('user')
  })

  it('flattens tool chain blocks without dropping earlier entries', () => {
    const message = mapRenderedMessageToChatMessage({
      id: 'msg-1',
      role: 'assistant',
      content: '',
      content_blocks: [
        {
          type: 'tool_chain',
          tool_calls: [
            { id: 'tool-1', tool_name: 'Read', server_name: 'builtin', tool_type: 'read', status: 'completed' },
          ],
        },
        {
          type: 'tool_chain',
          tool_calls: [
            { id: 'tool-2', tool_name: 'Write', server_name: 'builtin', tool_type: 'write', status: 'completed' },
          ],
        },
      ],
    })

    expect(message.toolCalls?.map((tool) => tool.id)).toEqual(['tool-1', 'tool-2'])
  })

  it('creates unique fallback ids when rendered messages omit ids', () => {
    const first = mapRenderedMessageToChatMessage({
      role: 'assistant',
      content: 'First',
    })
    const second = mapRenderedMessageToChatMessage({
      role: 'assistant',
      content: 'Second',
    })

    expect(first.id).not.toBe(second.id)
    expect(first.id.startsWith('ws-')).toBe(true)
    expect(second.id.startsWith('ws-')).toBe(true)
  })

  it('joins thinking blocks in order', () => {
    const message = mapRenderedMessageToChatMessage({
      id: 'msg-thinking',
      role: 'assistant',
      content: '',
      content_blocks: [
        { type: 'thinking', content: 'one ' },
        { type: 'thinking', content: 'two' },
      ],
    })

    expect(message.thinkingContent).toBe('one two')
  })

  it('preserves rendered content blocks from api messages', () => {
    const contentBlocks = [
      { type: 'text' as const, content: 'Rendered text' },
      { type: 'thinking' as const, content: 'hidden thought' },
      {
        type: 'tool_chain' as const,
        tool_calls: [
          {
            id: 'tool-rendered',
            tool_name: 'Read',
            server_name: 'builtin',
            tool_type: 'read',
            status: 'completed' as const,
          },
        ],
      },
    ]

    const [message] = mapApiMessages([
      {
        id: 'rendered-message',
        role: 'assistant',
        content: 'Rendered text',
        timestamp: '2026-05-30T00:00:00.000Z',
        content_blocks: contentBlocks,
      },
    ])

    expect(message.contentBlocks).toBe(contentBlocks)
    expect(message.toolCalls?.map((tool) => tool.id)).toEqual(['tool-rendered'])
    expect(message.thinkingContent).toBe('hidden thought')
  })

  it('pairs tool results by tool_use_id', () => {
    const [assistant] = mapApiMessages([
      {
        id: 'call-1',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'mcp__repo__read',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
      {
        id: 'call-2',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'mcp__repo__write',
        tool_use_id: 'tool-b',
        timestamp: '2026-05-30T00:00:01.000Z',
      },
      {
        id: 'result-1',
        role: 'user',
        content: '{"content":{"ok":true},"kind":"json","truncated":false}',
        content_type: 'tool_result',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:02.000Z',
      },
    ])

    expect(assistant.toolCalls?.map((tool) => [tool.id, tool.status])).toEqual([
      ['tool-a', 'completed'],
      ['tool-b', 'calling'],
    ])
    expect(assistant.toolCalls?.[0].result).toEqual({
      content: { ok: true },
      kind: 'json',
      truncated: false,
    })
  })

  it('marks invalid tool result payloads as errors', () => {
    const [assistant] = mapApiMessages([
      {
        id: 'call-1',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'Read',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
      {
        id: 'result-1',
        role: 'user',
        content: '{"ok":true}',
        content_type: 'tool_result',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:01.000Z',
      },
    ])

    expect(assistant.toolCalls?.[0]).toEqual(
      expect.objectContaining({
        error: 'Invalid tool result payload',
        status: 'error',
      }),
    )
    expect(assistant.toolCalls?.[0].result).toBeUndefined()
  })

  it('guards tool-use messages with invalid embedded tool result payloads', () => {
    const [assistant] = mapApiMessages([
      {
        id: 'call-1',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'Read',
        tool_input: '[]',
        tool_result: '{"ok":true}',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
    ])

    expect(assistant.toolCalls?.[0]).toEqual(
      expect.objectContaining({
        arguments: undefined,
        error: 'Invalid tool result payload',
        status: 'error',
      }),
    )
  })

  it('does not treat errored tool calls as pending', () => {
    expect(
      findPendingToolCall({
        id: 'assistant-1',
        role: 'assistant',
        content: '',
        timestamp: new Date('2026-05-30T00:00:00.000Z'),
        toolCalls: [
          {
            id: 'tool-a',
            tool_name: 'Read',
            server_name: 'builtin',
            tool_type: 'read',
            status: 'error',
            error: 'blocked',
          },
        ],
      }),
    ).toBeUndefined()
  })

  it('keeps consecutive tool calls in separate tool chain blocks', () => {
    const [assistant] = mapApiMessages([
      {
        id: 'call-1',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'Read',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
      {
        id: 'call-2',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'Write',
        tool_use_id: 'tool-b',
        timestamp: '2026-05-30T00:00:01.000Z',
      },
    ])

    expect(assistant.contentBlocks).toEqual([
      {
        type: 'tool_chain',
        tool_calls: [expect.objectContaining({ id: 'tool-a' })],
      },
      {
        type: 'tool_chain',
        tool_calls: [expect.objectContaining({ id: 'tool-b' })],
      },
    ])
  })

  it('attaches hook feedback to the last tool call as an error', () => {
    const [assistant] = mapApiMessages([
      {
        id: 'call-1',
        role: 'assistant',
        content: '',
        content_type: 'tool_use',
        tool_name: 'Read',
        tool_use_id: 'tool-a',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
      {
        id: 'feedback-1',
        role: 'user',
        content: 'Stop hook feedback: command blocked',
        timestamp: '2026-05-30T00:00:01.000Z',
      },
    ])

    expect(assistant.toolCalls?.[0]).toEqual(
      expect.objectContaining({
        error: 'Stop hook feedback: command blocked',
        status: 'error',
      }),
    )
    expect(assistant.contentBlocks?.[0]).toEqual({
      type: 'tool_chain',
      tool_calls: [
        expect.objectContaining({
          error: 'Stop hook feedback: command blocked',
          status: 'error',
        }),
      ],
    })
  })

  it('renders orphan hook feedback as a system message', () => {
    const [message] = mapApiMessages([
      {
        id: 'feedback-1',
        role: 'user',
        content: 'UserPromptSubmit hook feedback',
        timestamp: '2026-05-30T00:00:00.000Z',
      },
    ])

    expect(message).toEqual(
      expect.objectContaining({
        id: 'feedback-1',
        role: 'system',
        content: 'UserPromptSubmit hook feedback',
      }),
    )
  })
})
