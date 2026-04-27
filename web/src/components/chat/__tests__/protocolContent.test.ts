import { describe, expect, it } from 'vitest'

import codexProtocolLeakFixture from './fixtures/codex-protocol-leak.txt?raw'
import { hasProtocolToolContent, splitProtocolContent } from '../protocolContent'

describe('protocolContent', () => {
  it('parses nested protocol payloads into a tool-call segment', () => {
    const content =
      '<environment_context><workspace><path>/tmp/project</path></workspace></environment_context>'
    const [segment] = splitProtocolContent(content, 'msg-1')

    expect(segment.type).toBe('tool_call')
    if (segment.type !== 'tool_call') {
      throw new Error('Expected protocol tool call')
    }
    expect(segment.call.tool_name).toBe('protocol_context')
    expect(segment.call.result?.content_type).toBe('text')
    expect(String(segment.call.result?.content)).toContain('/tmp/project')
  })

  it('collapses Codex system instructions with nested fenced tags without visible leaks', () => {
    const content = [
      '<system_instructions>',
      'Outer instruction before fenced content.',
      '```xml',
      '<system_instructions>',
      'Nested instruction that must stay inside the protocol tool call.',
      '</system_instructions>',
      '```',
      'Outer instruction after fenced content.',
      '</system_instructions>',
    ].join('\n')

    const segments = splitProtocolContent(content, 'msg-nested')

    expect(segments).toHaveLength(1)
    const [segment] = segments
    expect(segment.type).toBe('tool_call')
    if (segment.type !== 'tool_call') {
      throw new Error('Expected protocol tool call')
    }
    expect(segment.call.tool_name).toBe('protocol_context')
    expect(String(segment.call.result?.content)).toContain(
      'Nested instruction that must stay inside the protocol tool call.',
    )
    expect(
      segments.some(
        (candidate) =>
          candidate.type === 'text' && candidate.content.includes('system_instructions'),
      ),
    ).toBe(false)
  })

  it('collapses the captured Codex developer fixture without leaking protocol tags', () => {
    const content = `<skills_instructions>\n${codexProtocolLeakFixture}\n</skills_instructions>`
    const segments = splitProtocolContent(content, 'codex-fixture')
    const leakedText = segments
      .map((segment) => (segment.type === 'text' ? segment.content : ''))
      .join('\n')
    const toolCallCount = segments.filter((segment) => segment.type === 'tool_call').length

    expect(toolCallCount).toBeGreaterThan(0)
    expect(leakedText).not.toMatch(
      /<\/?(?:permissions instructions|INSTRUCTIONS|skills_instructions)\b/i,
    )
  })

  it('treats unterminated protocol tags as plain text', () => {
    const content = '<environment_context><workspace>'
    const [segment] = splitProtocolContent(content, 'msg-2')

    expect(segment).toEqual({ type: 'text', content })
    expect(hasProtocolToolContent(content)).toBe(false)
  })
})
