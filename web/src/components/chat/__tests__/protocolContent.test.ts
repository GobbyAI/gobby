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
    expect(segment.call.result?.kind).toBe('text')
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

  it('ignores inline protocol tag examples while matching Codex preamble tags', () => {
    const content = [
      '<collaboration_mode>',
      'Use the literal `</collaboration_mode>` example without closing the outer block.',
      'The remaining preamble text must stay inside the protocol result.',
      '</collaboration_mode>',
    ].join('\n')

    const segments = splitProtocolContent(content, 'msg-inline')
    const visibleText = segments
      .map((segment) => (segment.type === 'text' ? segment.content : ''))
      .join('\n')

    expect(segments).toHaveLength(1)
    const [segment] = segments
    expect(segment.type).toBe('tool_call')
    if (segment.type !== 'tool_call') {
      throw new Error('Expected protocol tool call')
    }
    expect(String(segment.call.result?.content)).toContain(
      'The remaining preamble text must stay inside the protocol result.',
    )
    expect(visibleText.trim()).toBe('')
  })

  it('collapses the captured Codex developer fixture without visible preamble leaks', () => {
    const segments = splitProtocolContent(codexProtocolLeakFixture, 'codex-fixture')
    const visibleText = segments
      .map((segment) => (segment.type === 'text' ? segment.content : ''))
      .join('\n')
    const toolCallCount = segments.filter((segment) => segment.type === 'tool_call').length

    expect(toolCallCount).toBeGreaterThan(0)
    expect(visibleText.trim()).toBe('')
  })

  it('collapses upstream <plugins_instructions> blocks (and the singular spelling) without visible leaks', () => {
    for (const tag of ['plugins_instructions', 'plugin_instructions']) {
      const content = [
        `<${tag}>`,
        'Missing/blocked: callable capabilities for the plugin host.',
        `</${tag}>`,
      ].join('\n')

      const segments = splitProtocolContent(content, `msg-${tag}`)
      const visibleText = segments
        .map((segment) => (segment.type === 'text' ? segment.content : ''))
        .join('\n')

      expect(segments).toHaveLength(1)
      const [segment] = segments
      expect(segment.type).toBe('tool_call')
      if (segment.type !== 'tool_call') {
        throw new Error('Expected protocol tool call')
      }
      expect(segment.call.tool_name).toBe('protocol_context')
      expect(String(segment.call.result?.content)).toContain(
        'Missing/blocked: callable capabilities for the plugin host.',
      )
      expect(visibleText.trim()).toBe('')
    }
  })

  it('treats unterminated protocol tags as plain text', () => {
    const content = '<environment_context><workspace>'
    const [segment] = splitProtocolContent(content, 'msg-2')

    expect(segment).toEqual({ type: 'text', content })
    expect(hasProtocolToolContent(content)).toBe(false)
  })

  it('keeps ordinary markdown headings visible', () => {
    const content = [
      '# Release Notes',
      '',
      '## Platform Context',
      'A normal product update.',
      '',
      '## Capabilities',
      'Users can filter entries.',
      '',
      '## Interaction Style',
      'The UI stays compact.',
    ].join('\n')

    expect(splitProtocolContent(content, 'ordinary')).toEqual([{ type: 'text', content }])
    expect(hasProtocolToolContent(content)).toBe(false)
  })

  describe('inline wrapper tags and the streaming tail guard (#18343)', () => {
    it('strips complete <proposed_plan> tags while keeping the body visible', () => {
      const content = 'Intro.\n<proposed_plan>## Plan\n1. Do it</proposed_plan>\nOutro.'
      const segments = splitProtocolContent(content, 'msg-plan')
      const visibleText = segments
        .map((segment) => (segment.type === 'text' ? segment.content : ''))
        .join('\n')

      expect(visibleText).toContain('## Plan')
      expect(visibleText).toContain('1. Do it')
      expect(visibleText).not.toContain('<proposed_plan>')
      expect(visibleText).not.toContain('</proposed_plan>')
    })

    it('suppresses a trailing incomplete open-tag prefix while streaming', () => {
      // A tag split across stream chunks ("<proposed_" arrived, "plan>" has
      // not) must not flash as literal text mid-stream.
      const segments = splitProtocolContent('The plan follows. <proposed_', 'msg-tail', true)

      expect(segments).toEqual([{ type: 'text', content: 'The plan follows. ' }])
    })

    it('suppresses a trailing incomplete close-tag prefix while streaming', () => {
      const segments = splitProtocolContent('body text</proposed_pla', 'msg-tail-close', true)

      expect(segments).toEqual([{ type: 'text', content: 'body text' }])
    })

    it('keeps a trailing tag-like literal once the message is complete', () => {
      const content = 'Escaping example ends with <proposed_'
      expect(splitProtocolContent(content, 'msg-done', false)).toEqual([
        { type: 'text', content },
      ])
    })

    it('leaves a non-tag trailing angle fragment visible while streaming', () => {
      const content = 'compare a <b'
      expect(splitProtocolContent(content, 'msg-lt', true)).toEqual([{ type: 'text', content }])
    })
  })
})
