import { describe, it, expect } from 'vitest'
import type { ToolCall } from '../../../types/chat'
import { groupToolCalls, extractBase64Image } from '../ToolCallCard'
import type { ToolCallGroup, ToolCallSingle } from '../ToolCallCard'

function makeCall(overrides: Partial<ToolCall> & { id: string; tool_name: string }): ToolCall {
  return {
    server_name: 'builtin',
    status: 'completed',
    ...overrides,
  }
}

describe('groupToolCalls', () => {
  it('returns empty array for empty input', () => {
    expect(groupToolCalls([])).toEqual([])
  })

  it('returns single segment for one call', () => {
    const call = makeCall({ id: '1', tool_name: 'Read' })
    const result = groupToolCalls([call])
    expect(result).toHaveLength(1)
    expect(result[0].kind).toBe('single')
    expect((result[0] as ToolCallSingle).call).toBe(call)
  })

  it('groups 2 consecutive same-type calls', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(1)
    expect(result[0].kind).toBe('group')
    const group = result[0] as ToolCallGroup
    expect(group.calls).toHaveLength(2)
    expect(group.toolName).toBe('Read')
    expect(group.displayName).toBe('Read')
  })

  it('groups 3 consecutive same-type calls', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Read' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(1)
    const group = result[0] as ToolCallGroup
    expect(group.calls).toHaveLength(3)
  })

  it('does not group different tool types', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Bash' }),
      makeCall({ id: '3', tool_name: 'Edit' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(3)
    expect(result.every(s => s.kind === 'single')).toBe(true)
  })

  it('groups interleaved runs correctly', () => {
    // [Read, Read, Read, Bash, Bash, Edit]
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Read' }),
      makeCall({ id: '4', tool_name: 'Bash' }),
      makeCall({ id: '5', tool_name: 'Bash' }),
      makeCall({ id: '6', tool_name: 'Edit' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(3)
    expect(result[0].kind).toBe('group')
    expect((result[0] as ToolCallGroup).calls).toHaveLength(3)
    expect((result[0] as ToolCallGroup).displayName).toBe('Read')
    expect(result[1].kind).toBe('group')
    expect((result[1] as ToolCallGroup).calls).toHaveLength(2)
    expect((result[1] as ToolCallGroup).displayName).toBe('Bash')
    expect(result[2].kind).toBe('single')
  })

  it('never groups render_surface calls', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'render_surface' }),
      makeCall({ id: '2', tool_name: 'render_surface' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(2)
    expect(result.every(s => s.kind === 'single')).toBe(true)
  })

  it('never groups AskUserQuestion calls', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'AskUserQuestion' }),
      makeCall({ id: '2', tool_name: 'AskUserQuestion' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(2)
    expect(result.every(s => s.kind === 'single')).toBe(true)
  })

  it('never groups pending_approval calls', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Bash', status: 'pending_approval' }),
      makeCall({ id: '2', tool_name: 'Bash', status: 'pending_approval' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(2)
    expect(result.every(s => s.kind === 'single')).toBe(true)
  })

  it('splits group when pending_approval appears mid-run', () => {
    // [Read, Read, pending_approval Edit, Read, Read]
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Edit', status: 'pending_approval' }),
      makeCall({ id: '4', tool_name: 'Read' }),
      makeCall({ id: '5', tool_name: 'Read' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(3)
    expect(result[0].kind).toBe('group')
    expect((result[0] as ToolCallGroup).calls).toHaveLength(2)
    expect(result[1].kind).toBe('single')
    expect((result[1] as ToolCallSingle).call.status).toBe('pending_approval')
    expect(result[2].kind).toBe('group')
    expect((result[2] as ToolCallGroup).calls).toHaveLength(2)
  })

  it('sets hasErrors when any call in group has error status', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read', status: 'completed' }),
      makeCall({ id: '2', tool_name: 'Read', status: 'error' }),
      makeCall({ id: '3', tool_name: 'Read', status: 'completed' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(1)
    const group = result[0] as ToolCallGroup
    expect(group.hasErrors).toBe(true)
    expect(group.allCompleted).toBe(false)
  })

  it('sets allCompleted when every call is completed', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read', status: 'completed' }),
      makeCall({ id: '2', tool_name: 'Read', status: 'completed' }),
    ]
    const result = groupToolCalls(calls)
    const group = result[0] as ToolCallGroup
    expect(group.allCompleted).toBe(true)
    expect(group.hasErrors).toBe(false)
    expect(group.hasInFlight).toBe(false)
  })

  it('sets hasInFlight when any call is still calling', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read', status: 'completed' }),
      makeCall({ id: '2', tool_name: 'Read', status: 'calling' }),
    ]
    const result = groupToolCalls(calls)
    const group = result[0] as ToolCallGroup
    expect(group.hasInFlight).toBe(true)
    expect(group.allCompleted).toBe(false)
  })

  it('formats MCP proxy tool names in displayName', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'mcp__gobby__list_tools' }),
      makeCall({ id: '2', tool_name: 'mcp__gobby__list_tools' }),
    ]
    const result = groupToolCalls(calls)
    const group = result[0] as ToolCallGroup
    expect(group.displayName).toBe('list_tools')
    expect(group.toolName).toBe('mcp__gobby__list_tools')
  })

  it('breaks group when ungroupable call interrupts same-type run', () => {
    // [Read, Read, AskUserQuestion, Read, Read]
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'AskUserQuestion', status: 'calling' }),
      makeCall({ id: '4', tool_name: 'Read' }),
      makeCall({ id: '5', tool_name: 'Read' }),
    ]
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(3)
    expect(result[0].kind).toBe('group')
    expect(result[1].kind).toBe('single')
    expect(result[2].kind).toBe('group')
  })

  it('handles 10 consecutive same-type calls', () => {
    const calls = Array.from({ length: 10 }, (_, i) =>
      makeCall({ id: String(i), tool_name: 'Read' })
    )
    const result = groupToolCalls(calls)
    expect(result).toHaveLength(1)
    expect(result[0].kind).toBe('group')
    expect((result[0] as ToolCallGroup).calls).toHaveLength(10)
  })
})

describe('extractBase64Image', () => {
  it('returns null for non-image values', () => {
    expect(extractBase64Image(null)).toBeNull()
    expect(extractBase64Image(undefined)).toBeNull()
    expect(extractBase64Image(42)).toBeNull()
    expect(extractBase64Image('just a string')).toBeNull()
    expect(extractBase64Image({ foo: 'bar' })).toBeNull()
  })

  it('detects data URI strings', () => {
    const uri = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=='
    expect(extractBase64Image(uri)).toBe(uri)
  })

  it('detects jpeg data URI', () => {
    const uri = 'data:image/jpeg;base64,/9j/4AAQ=='
    expect(extractBase64Image(uri)).toBe(uri)
  })

  it('detects svg+xml data URI', () => {
    const uri = 'data:image/svg+xml;base64,PHN2Zz4='
    expect(extractBase64Image(uri)).toBe(uri)
  })

  it('detects MCP/Anthropic image content block', () => {
    const result = {
      type: 'image',
      source: { type: 'base64', media_type: 'image/png', data: 'iVBORw0KGgo=' },
    }
    expect(extractBase64Image(result)).toBe('data:image/png;base64,iVBORw0KGgo=')
  })

  it('detects image in array of content blocks', () => {
    const result = [
      { type: 'text', text: 'Here is the screenshot' },
      { type: 'image', source: { type: 'base64', media_type: 'image/jpeg', data: '/9j/4AAQ==' } },
    ]
    expect(extractBase64Image(result)).toBe('data:image/jpeg;base64,/9j/4AAQ==')
  })

  it('returns null for malformed image objects', () => {
    expect(extractBase64Image({ type: 'image' })).toBeNull()
    expect(extractBase64Image({ type: 'image', source: null })).toBeNull()
    expect(extractBase64Image({ type: 'image', source: { type: 'url' } })).toBeNull()
    expect(extractBase64Image({ type: 'image', source: { type: 'base64', data: 123 } })).toBeNull()
  })
})
