import { describe, expect, it } from 'vitest'

import { looksLikeSystemBootstrapText, normalizeChatRole } from '../chatMessageMapping'

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
})
