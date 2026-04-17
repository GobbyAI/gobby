import { describe, expect, it } from 'vitest'

import { looksLikeSystemBootstrapText, mapRenderedMessageToChatMessage, normalizeChatRole } from '../chatMessageMapping'

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
})
