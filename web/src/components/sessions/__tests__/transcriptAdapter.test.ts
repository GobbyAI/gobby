import { describe, it, expect } from 'vitest'
import { sessionMessagesToChatMessages } from '../transcriptAdapter'
import type { SessionMessage } from '../../../hooks/useSessionDetail'

function msg(overrides: Partial<SessionMessage> & { id: string; role: string }): SessionMessage {
  return {
    content: '',
    timestamp: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('sessionMessagesToChatMessages', () => {
  it('returns empty array for empty input', () => {
    expect(sessionMessagesToChatMessages([])).toEqual([])
  })

  it('converts a simple user message', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'user', content: 'hello' }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].role).toBe('user')
    expect(result[0].content).toBe('hello')
    expect(result[0].id).toBe('1')
  })

  it('converts a simple assistant message', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'hi there' }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].role).toBe('assistant')
    expect(result[0].content).toBe('hi there')
  })

  it('maps unknown roles to system', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'system', content: 'system msg' }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].role).toBe('system')
  })

  it('skips empty assistant messages', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: '' }),
      msg({ id: '2', role: 'assistant', content: '  ' }),
    ])
    expect(result).toHaveLength(0)
  })

  it('skips empty user messages', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'user', content: '' }),
    ])
    expect(result).toHaveLength(0)
  })

  it('attaches tool calls to the preceding assistant message', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'Let me check' }),
      msg({
        id: '2',
        role: 'assistant',
        tool_name: 'read_file',
        tool_input: '{"path": "/foo"}',
        tool_result: '"file contents"',
      }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].toolCalls).toHaveLength(1)
    expect(result[0].toolCalls![0]).toMatchObject({
      id: 'tool-2',
      tool_name: 'read_file',
      server_name: '',
      status: 'completed',
      arguments: { path: '/foo' },
      result: 'file contents',
    })
  })

  it('extracts server name from MCP proxy tool names', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'calling tool' }),
      msg({
        id: '2',
        role: 'assistant',
        tool_name: 'mcp__gobby__create_task',
        tool_input: '{"title": "test"}',
      }),
    ])
    expect(result[0].toolCalls![0].server_name).toBe('gobby')
  })

  it('handles malformed tool_input JSON gracefully', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'calling' }),
      msg({
        id: '2',
        role: 'assistant',
        tool_name: 'some_tool',
        tool_input: 'not json',
      }),
    ])
    expect(result[0].toolCalls![0].arguments).toBeUndefined()
  })

  it('returns raw string for non-JSON tool_result', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'calling' }),
      msg({
        id: '2',
        role: 'assistant',
        tool_name: 'some_tool',
        tool_result: 'plain text result',
      }),
    ])
    expect(result[0].toolCalls![0].result).toBe('plain text result')
  })

  it('drops tool messages with no preceding assistant message', () => {
    const result = sessionMessagesToChatMessages([
      msg({
        id: '1',
        role: 'assistant',
        tool_name: 'orphan_tool',
      }),
    ])
    // Tool message is consumed but not attached anywhere; no messages produced
    expect(result).toHaveLength(0)
  })

  it('skips user-role tool_result protocol messages', () => {
    const result = sessionMessagesToChatMessages([
      msg({
        id: '1',
        role: 'user',
        content: '[{"type":"tool_result","tool_use_id":"123","content":"ok"}]',
      }),
    ])
    expect(result).toHaveLength(0)
  })

  it('parses assistant messages with JSON tool_use arrays', () => {
    const toolUseJson = JSON.stringify([
      { type: 'tool_use', id: 'tu_1', name: 'mcp__github__list_prs', input: { repo: 'foo' } },
      { type: 'tool_use', id: 'tu_2', name: 'read_file', input: { path: '/bar' } },
    ])
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: toolUseJson }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].content).toBe('')
    expect(result[0].toolCalls).toHaveLength(2)
    expect(result[0].toolCalls![0]).toMatchObject({
      id: 'tu_1',
      tool_name: 'mcp__github__list_prs',
      server_name: 'github',
      status: 'completed',
      arguments: { repo: 'foo' },
    })
    expect(result[0].toolCalls![1]).toMatchObject({
      id: 'tu_2',
      tool_name: 'read_file',
      server_name: '',
    })
  })

  it('falls through to normal handling for JSON that is not tool_use', () => {
    const content = '[{"type":"text","text":"hello"}]'
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content }),
    ])
    // No tool_use blocks found, so falls through. Content starts with [{ but
    // no tool_use entries means it falls through to the "skip empty" or text check
    expect(result).toHaveLength(1)
    expect(result[0].content).toBe(content)
  })

  it('falls through on invalid JSON in assistant tool_use block', () => {
    const content = '[{invalid json tool_use'
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].content).toBe(content)
  })

  it('handles tool_use with missing fields gracefully', () => {
    const toolUseJson = JSON.stringify([
      { type: 'tool_use' }, // no id, no name, no input
    ])
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: toolUseJson }),
    ])
    expect(result[0].toolCalls![0]).toMatchObject({
      tool_name: 'unknown',
      server_name: '',
    })
  })

  it('handles multiple tool calls on one assistant message', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'Working on it' }),
      msg({ id: '2', role: 'assistant', tool_name: 'tool_a' }),
      msg({ id: '3', role: 'assistant', tool_name: 'tool_b' }),
      msg({ id: '4', role: 'assistant', tool_name: 'tool_c' }),
    ])
    expect(result).toHaveLength(1)
    expect(result[0].toolCalls).toHaveLength(3)
  })

  it('handles interleaved assistant and tool messages', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'user', content: 'do stuff' }),
      msg({ id: '2', role: 'assistant', content: 'step 1' }),
      msg({ id: '3', role: 'assistant', tool_name: 'tool_a' }),
      msg({ id: '4', role: 'assistant', content: 'step 2' }),
      msg({ id: '5', role: 'assistant', tool_name: 'tool_b' }),
    ])
    expect(result).toHaveLength(3)
    expect(result[0].content).toBe('do stuff')
    expect(result[1].content).toBe('step 1')
    expect(result[1].toolCalls).toHaveLength(1)
    expect(result[2].content).toBe('step 2')
    expect(result[2].toolCalls).toHaveLength(1)
  })

  it('handles null content', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: undefined as unknown as string }),
    ])
    expect(result).toHaveLength(0)
  })

  it('parses tool_input that is a JSON primitive (not object)', () => {
    const result = sessionMessagesToChatMessages([
      msg({ id: '1', role: 'assistant', content: 'calling' }),
      msg({
        id: '2',
        role: 'assistant',
        tool_name: 'tool',
        tool_input: '"just a string"',
      }),
    ])
    // Parsed but not an object, so returns undefined
    expect(result[0].toolCalls![0].arguments).toBeUndefined()
  })
})
