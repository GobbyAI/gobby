import { describe, it, expect } from 'vitest'
import type { ToolCall } from '../../../types/chat'
import {
  formatToolName,
  truncStr,
  pathBasename,
  getToolSummary,
  parseReadOutput,
  parseGrepOutput,
  getLanguageFromPath,
  computeLineDiff,
  buildChainSummary,
} from '../ToolCallCard'

function makeCall(overrides: Partial<ToolCall> & { id: string; tool_name: string }): ToolCall {
  return {
    server_name: 'builtin',
    status: 'completed',
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// formatToolName
// ---------------------------------------------------------------------------
describe('formatToolName', () => {
  it('returns last segment of double-underscore name', () => {
    expect(formatToolName('mcp__gobby__list_tools')).toBe('list_tools')
  })

  it('returns the name itself when no underscores', () => {
    expect(formatToolName('Read')).toBe('Read')
  })

  it('returns last segment for two-part names', () => {
    expect(formatToolName('mcp__call_tool')).toBe('call_tool')
  })

  it('returns original for empty string', () => {
    expect(formatToolName('')).toBe('')
  })
})

// ---------------------------------------------------------------------------
// truncStr
// ---------------------------------------------------------------------------
describe('truncStr', () => {
  it('returns null for null/undefined/empty', () => {
    expect(truncStr(null, 10)).toBeNull()
    expect(truncStr(undefined, 10)).toBeNull()
    expect(truncStr('', 10)).toBeNull()
  })

  it('returns string unchanged when shorter than max', () => {
    expect(truncStr('hello', 10)).toBe('hello')
  })

  it('returns string unchanged when exactly max length', () => {
    expect(truncStr('hello', 5)).toBe('hello')
  })

  it('truncates and adds ellipsis when longer than max', () => {
    expect(truncStr('hello world', 6)).toBe('hello\u2026')
  })
})

// ---------------------------------------------------------------------------
// pathBasename
// ---------------------------------------------------------------------------
describe('pathBasename', () => {
  it('returns last path component', () => {
    expect(pathBasename('/home/user/file.ts')).toBe('file.ts')
  })

  it('returns the path itself if no slashes', () => {
    expect(pathBasename('file.ts')).toBe('file.ts')
  })

  it('returns original for empty string', () => {
    expect(pathBasename('')).toBe('')
  })

  it('handles trailing slash', () => {
    // path.split('/') produces ['...', ''], last element is '', falls back to path
    expect(pathBasename('/home/user/')).toBe('/home/user/')
  })
})

// ---------------------------------------------------------------------------
// getToolSummary
// ---------------------------------------------------------------------------
describe('getToolSummary', () => {
  it('returns file_path for Read tool', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Read',
      arguments: { file_path: '/src/main.ts' },
    })
    expect(getToolSummary(call)).toBe('/src/main.ts')
  })

  it('returns file_path for Write tool', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Write',
      arguments: { file_path: '/src/out.ts' },
    })
    expect(getToolSummary(call)).toBe('/src/out.ts')
  })

  it('returns file_path for Edit tool', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Edit',
      arguments: { file_path: '/src/edit.ts' },
    })
    expect(getToolSummary(call)).toBe('/src/edit.ts')
  })

  it('returns truncated command for Bash', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Bash',
      arguments: { command: 'echo hello' },
    })
    expect(getToolSummary(call)).toBe('echo hello')
  })

  it('returns pattern for Grep without path', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Grep',
      arguments: { pattern: 'TODO' },
    })
    expect(getToolSummary(call)).toBe('"TODO"')
  })

  it('returns pattern and path for Grep with path', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Grep',
      arguments: { pattern: 'TODO', path: 'src/' },
    })
    expect(getToolSummary(call)).toBe('"TODO" in src/')
  })

  it('returns null for Grep without pattern', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Grep',
      arguments: {},
    })
    expect(getToolSummary(call)).toBeNull()
  })

  it('returns pattern for Glob', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Glob',
      arguments: { pattern: '**/*.ts' },
    })
    expect(getToolSummary(call)).toBe('**/*.ts')
  })

  it('returns agent type and description for Task', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Task',
      arguments: { subagent_type: 'Explore', description: 'Find files' },
    })
    expect(getToolSummary(call)).toBe('Explore (Find files)')
  })

  it('returns null for Task without subagent_type', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Task',
      arguments: {},
    })
    expect(getToolSummary(call)).toBeNull()
  })

  it('returns url for WebFetch', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'WebFetch',
      arguments: { url: 'https://example.com' },
    })
    expect(getToolSummary(call)).toBe('https://example.com')
  })

  it('returns query for WebSearch', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'WebSearch',
      arguments: { query: 'vitest setup' },
    })
    expect(getToolSummary(call)).toBe('"vitest setup"')
  })

  it('returns null for list_mcp_servers', () => {
    const call = makeCall({ id: '1', tool_name: 'list_mcp_servers', arguments: {} })
    expect(getToolSummary(call)).toBeNull()
  })

  it('returns null for ExitPlanMode', () => {
    const call = makeCall({ id: '1', tool_name: 'ExitPlanMode', arguments: {} })
    expect(getToolSummary(call)).toBeNull()
  })

  it('returns server_name for list_tools', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'list_tools',
      arguments: { server_name: 'gobby' },
    })
    expect(getToolSummary(call)).toBe('gobby')
  })

  it('returns server.tool for get_tool_schema', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'get_tool_schema',
      arguments: { server_name: 'gobby', tool_name: 'create_task' },
    })
    expect(getToolSummary(call)).toBe('gobby.create_task')
  })

  it('returns server.tool for call_tool', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'call_tool',
      arguments: { server_name: 'gobby', tool_name: 'create_task' },
    })
    expect(getToolSummary(call)).toBe('gobby.create_task')
  })

  it('returns server.name for unknown tools from non-builtin servers', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'custom_tool',
      server_name: 'my-server',
      arguments: {},
    })
    expect(getToolSummary(call)).toBe('my-server.custom_tool')
  })

  it('returns null for unknown builtin tools', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'unknown_tool',
      server_name: 'builtin',
      arguments: {},
    })
    expect(getToolSummary(call)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// parseReadOutput
// ---------------------------------------------------------------------------
describe('parseReadOutput', () => {
  it('parses numbered lines with arrow separator', () => {
    const input = '  1\u2192const x = 1\n  2\u2192const y = 2'
    const result = parseReadOutput(input)
    expect(result).not.toBeNull()
    expect(result!.startLine).toBe(1)
    expect(result!.content).toBe('const x = 1\nconst y = 2')
  })

  it('detects start line from first numbered line', () => {
    const input = ' 10\u2192line ten\n 11\u2192line eleven'
    const result = parseReadOutput(input)
    expect(result).not.toBeNull()
    expect(result!.startLine).toBe(10)
  })

  it('returns null for non-matching format', () => {
    expect(parseReadOutput('just plain text\nmore text')).toBeNull()
  })

  it('returns object with empty content for empty input', () => {
    // Empty string splits to [''] which is treated as a blank line
    const result = parseReadOutput('')
    expect(result).not.toBeNull()
    expect(result!.content).toBe('')
  })

  it('handles blank lines in output', () => {
    const input = '  1\u2192line one\n\n  3\u2192line three'
    const result = parseReadOutput(input)
    expect(result).not.toBeNull()
    expect(result!.content).toBe('line one\n\nline three')
  })
})

// ---------------------------------------------------------------------------
// parseGrepOutput
// ---------------------------------------------------------------------------
describe('parseGrepOutput', () => {
  it('parses single file results', () => {
    const input = 'src/main.ts:10:const x = 1\nsrc/main.ts:20:const y = 2'
    const result = parseGrepOutput(input)
    expect(result).not.toBeNull()
    expect(result).toHaveLength(1)
    expect(result![0].filePath).toBe('src/main.ts')
    expect(result![0].lines).toHaveLength(2)
    expect(result![0].lines[0]).toEqual({ lineNum: 10, content: 'const x = 1' })
  })

  it('groups by file path', () => {
    const input = 'a.ts:1:foo\na.ts:2:bar\n--\nb.ts:5:baz'
    const result = parseGrepOutput(input)
    expect(result).not.toBeNull()
    expect(result).toHaveLength(2)
    expect(result![0].filePath).toBe('a.ts')
    expect(result![1].filePath).toBe('b.ts')
  })

  it('handles -- separators between groups', () => {
    const input = 'a.ts:1:foo\n--\na.ts:10:bar'
    const result = parseGrepOutput(input)
    expect(result).not.toBeNull()
    // After --, it starts a new group for the same file
    expect(result!.length).toBeGreaterThanOrEqual(1)
  })

  it('returns null for empty input', () => {
    expect(parseGrepOutput('')).toBeNull()
  })

  it('returns null for non-matching format', () => {
    expect(parseGrepOutput('no colons here')).toBeNull()
  })

  it('handles context lines with - separator', () => {
    const input = 'a.ts:5:match line\na.ts:6-context line'
    const result = parseGrepOutput(input)
    expect(result).not.toBeNull()
    expect(result![0].lines).toHaveLength(2)
  })
})

// ---------------------------------------------------------------------------
// getLanguageFromPath
// ---------------------------------------------------------------------------
describe('getLanguageFromPath', () => {
  it('maps .py to python', () => {
    expect(getLanguageFromPath('main.py')).toBe('python')
  })

  it('maps .ts to typescript', () => {
    expect(getLanguageFromPath('index.ts')).toBe('typescript')
  })

  it('maps .tsx to tsx', () => {
    expect(getLanguageFromPath('App.tsx')).toBe('tsx')
  })

  it('maps .js to javascript', () => {
    expect(getLanguageFromPath('bundle.js')).toBe('javascript')
  })

  it('maps .json to json', () => {
    expect(getLanguageFromPath('package.json')).toBe('json')
  })

  it('maps .rs to rust', () => {
    expect(getLanguageFromPath('main.rs')).toBe('rust')
  })

  it('maps .go to go', () => {
    expect(getLanguageFromPath('main.go')).toBe('go')
  })

  it('maps .sh and .bash to bash', () => {
    expect(getLanguageFromPath('run.sh')).toBe('bash')
    expect(getLanguageFromPath('setup.bash')).toBe('bash')
  })

  it('maps .svg to xml', () => {
    expect(getLanguageFromPath('icon.svg')).toBe('xml')
  })

  it('returns "text" for unknown extensions', () => {
    expect(getLanguageFromPath('file.xyz')).toBe('text')
  })

  it('handles full paths', () => {
    expect(getLanguageFromPath('/home/user/project/src/main.py')).toBe('python')
  })
})

// ---------------------------------------------------------------------------
// computeLineDiff
// ---------------------------------------------------------------------------
describe('computeLineDiff', () => {
  it('returns all keep for identical strings', () => {
    const diff = computeLineDiff('a\nb\nc', 'a\nb\nc')
    expect(diff).toEqual([
      { type: 'keep', line: 'a' },
      { type: 'keep', line: 'b' },
      { type: 'keep', line: 'c' },
    ])
  })

  it('detects added lines', () => {
    const diff = computeLineDiff('a\nc', 'a\nb\nc')
    const added = diff.filter(d => d.type === 'add')
    expect(added).toHaveLength(1)
    expect(added[0].line).toBe('b')
  })

  it('detects removed lines', () => {
    const diff = computeLineDiff('a\nb\nc', 'a\nc')
    const removed = diff.filter(d => d.type === 'remove')
    expect(removed).toHaveLength(1)
    expect(removed[0].line).toBe('b')
  })

  it('handles complete replacement', () => {
    const diff = computeLineDiff('old', 'new')
    expect(diff).toContainEqual({ type: 'remove', line: 'old' })
    expect(diff).toContainEqual({ type: 'add', line: 'new' })
  })

  it('handles empty old string', () => {
    const diff = computeLineDiff('', 'new line')
    expect(diff).toContainEqual({ type: 'add', line: 'new line' })
  })

  it('handles empty new string', () => {
    const diff = computeLineDiff('old line', '')
    expect(diff).toContainEqual({ type: 'remove', line: 'old line' })
  })

  it('handles multi-line modifications', () => {
    const diff = computeLineDiff('a\nb\nc\nd', 'a\nB\nC\nd')
    // a and d kept, b and c removed, B and C added
    const keeps = diff.filter(d => d.type === 'keep')
    expect(keeps.map(d => d.line)).toContain('a')
    expect(keeps.map(d => d.line)).toContain('d')
  })
})

// ---------------------------------------------------------------------------
// buildChainSummary
// ---------------------------------------------------------------------------
describe('buildChainSummary', () => {
  it('returns empty string for no calls', () => {
    expect(buildChainSummary([])).toBe('')
  })

  it('returns single tool name', () => {
    const calls = [makeCall({ id: '1', tool_name: 'Read' })]
    expect(buildChainSummary(calls)).toBe('Read')
  })

  it('counts multiple calls of same type', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Read' }),
    ]
    expect(buildChainSummary(calls)).toBe('3 Read')
  })

  it('lists different tools separated by commas', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Bash' }),
      makeCall({ id: '3', tool_name: 'Edit' }),
    ]
    expect(buildChainSummary(calls)).toBe('Read, Bash, Edit')
  })

  it('mixes counted and single tools', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Read' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Bash' }),
    ]
    expect(buildChainSummary(calls)).toBe('2 Read, Bash')
  })

  it('formats MCP proxy tool names', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'mcp__gobby__list_tools' }),
      makeCall({ id: '2', tool_name: 'mcp__gobby__call_tool' }),
    ]
    expect(buildChainSummary(calls)).toBe('list_tools, call_tool')
  })
})
