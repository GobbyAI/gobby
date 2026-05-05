import { describe, it, expect } from 'vitest'
import type { ToolCall } from '../../../types/chat'
import { classifyTool } from '../../../types/chat'
import {
  computeLineDiff,
  defaultExpandedForCall,
  extractResultContent,
  extractResultMetadata,
  formatToolName,
  getToolDisplayName,
  getLanguageFromPath,
  getToolSummary,
  groupToolCalls,
  isReadOnlyBash,
  isReadOnlyMcp,
  parseGrepOutput,
  parseGsqzWrapper,
  parseReadOutput,
  pathBasename,
  truncStr,
  unwrapMcpResultEnvelope,
} from '../ToolCallCard.helpers'

function makeCall(overrides: Partial<ToolCall> & { id: string; tool_name: string }): ToolCall {
  return {
    server_name: 'builtin',
    status: 'completed',
    tool_type: classifyTool(overrides.tool_name),
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

  it('returns protocol tag summaries for protocol tool calls', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'protocol_context',
      tool_type: 'protocol',
      arguments: { tag: 'environment_context' },
    })
    expect(getToolSummary(call)).toBe('environment_context')
    expect(getToolDisplayName(call)).toBe('Protocol')
  })

  it('returns cmd-based command summaries for exec_command', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'exec_command',
      arguments: { cmd: 'git status --short' },
    })
    expect(getToolSummary(call)).toBe('git status --short')
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

describe('extractResultContent', () => {
  it('flattens MCP proxy envelopes to a single success object', () => {
    const result = {
      content: {
        success: true,
        result: { success: true },
        response_time_ms: 42,
      },
      content_type: 'json',
      truncated: false,
    }

    expect(extractResultContent(result)).toEqual({
      success: true,
      response_time_ms: 42,
    })
  })

  it('parses and flattens stringified MCP proxy envelopes', () => {
    const result = {
      content: '{"success":true,"result":{"task_id":"#11820"},"response_time_ms":42}',
      content_type: 'text',
      truncated: false,
      metadata: { source: 'mcp' },
    }

    expect(extractResultContent(result)).toEqual({
      task_id: '#11820',
      response_time_ms: 42,
    })
    expect(extractResultMetadata(result)).toEqual({
      source: 'mcp',
      response_time_ms: 42,
    })
  })

  it('flattens legacy Codex output-wrapped MCP envelopes', () => {
    const result = {
      content: {
        output: {
          success: true,
          result: { success: true },
          response_time_ms: 42,
        },
      },
      content_type: 'json',
      truncated: false,
    }

    expect(extractResultContent(result)).toEqual({
      success: true,
      response_time_ms: 42,
    })
  })

  it('flattens legacy Codex output-wrapped MCP failures', () => {
    const result = {
      content: {
        output: {
          success: false,
          result: {
            success: false,
            error: 'Rule enforced by Gobby: [consecutive-tool-block]',
            error_code: 'TOOL_BLOCKED',
          },
          response_time_ms: 1.59,
        },
      },
      content_type: 'json',
      truncated: false,
    }

    expect(extractResultContent(result)).toEqual({
      success: false,
      error: 'Rule enforced by Gobby: [consecutive-tool-block]',
      error_code: 'TOOL_BLOCKED',
      response_time_ms: 1.59,
    })
  })

  it('leaves non-envelope results untouched', () => {
    const payload = { success: true, result: { success: true } }
    const result = {
      content: payload,
      content_type: 'json',
      truncated: false,
    }

    expect(extractResultContent(result)).toEqual(payload)
    expect(extractResultMetadata(result)).toBeUndefined()
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

describe('getToolDisplayName', () => {
  it('canonicalizes exec_command to Bash', () => {
    const call = makeCall({ id: '1', tool_name: 'exec_command' })
    expect(getToolDisplayName(call)).toBe('Bash')
  })

  it('preserves non-shell tool names', () => {
    const call = makeCall({ id: '1', tool_name: 'Read' })
    expect(getToolDisplayName(call)).toBe('Read')
  })
})

// ---------------------------------------------------------------------------
// parseGsqzWrapper
// ---------------------------------------------------------------------------
describe('parseGsqzWrapper', () => {
  it('returns null when no wrapper header is present', () => {
    expect(parseGsqzWrapper('hello world\nline two')).toBeNull()
  })

  it('parses the gsqz fallback header', () => {
    const text = '[Output compressed by gsqz — fallback, 99% reduction]\n[gsqz:passthrough]\nbody line 1\nbody line 2'
    const result = parseGsqzWrapper(text)
    expect(result).not.toBeNull()
    expect(result!.metadata.strategy).toBe('fallback')
    expect(result!.metadata.reduction).toBe('99% reduction')
    expect(result!.body).toBe('body line 1\nbody line 2')
  })

  it('parses the chunked-output header with full metadata', () => {
    const text =
      'Chunk ID: beae62\nWall time: 0.0123 seconds\nProcess exited with code 0\nOriginal token count: 2174\nOutput:\nfrom __future__ import annotations\n'
    const result = parseGsqzWrapper(text)
    expect(result).not.toBeNull()
    expect(result!.metadata.chunkId).toBe('beae62')
    expect(result!.metadata.wallTimeSeconds).toBeCloseTo(0.0123)
    expect(result!.metadata.exitCode).toBe(0)
    expect(result!.metadata.tokenCount).toBe(2174)
    expect(result!.body).toBe('from __future__ import annotations\n')
  })

  it('parses the short chunked header (no exit code or token count)', () => {
    const text = 'Chunk ID: 21a8f9\nWall time: 0.1813 seconds\nOutput:\nhash ok? True\n'
    const result = parseGsqzWrapper(text)
    expect(result).not.toBeNull()
    expect(result!.metadata.chunkId).toBe('21a8f9')
    expect(result!.metadata.wallTimeSeconds).toBeCloseTo(0.1813)
    expect(result!.metadata.exitCode).toBeUndefined()
    expect(result!.metadata.tokenCount).toBeUndefined()
    expect(result!.body).toBe('hash ok? True\n')
  })

  it('returns null for empty or non-string input', () => {
    expect(parseGsqzWrapper('')).toBeNull()
    expect(parseGsqzWrapper(null as unknown as string)).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// unwrapMcpResultEnvelope
// ---------------------------------------------------------------------------
describe('unwrapMcpResultEnvelope', () => {
  it('returns null for primitives and arrays', () => {
    expect(unwrapMcpResultEnvelope('plain string')).toBeNull()
    expect(unwrapMcpResultEnvelope(42)).toBeNull()
    expect(unwrapMcpResultEnvelope(null)).toBeNull()
    expect(unwrapMcpResultEnvelope([1, 2, 3])).toBeNull()
  })

  it('extracts the output field as primary and surfaces the rest as meta', () => {
    const envelope = {
      output: 'tool stdout',
      session_id: 12345,
      project_id: 'abc-123',
    }
    const result = unwrapMcpResultEnvelope(envelope)
    expect(result).not.toBeNull()
    expect(result!.primary).toBe('tool stdout')
    expect(result!.meta).toEqual({ session_id: 12345, project_id: 'abc-123' })
  })

  it('extracts the content field for tool-result-style envelopes', () => {
    const envelope = { content: 'diff --git a/x.py b/x.py\n@@', is_error: false }
    const result = unwrapMcpResultEnvelope(envelope)
    expect(result).not.toBeNull()
    expect(result!.primary).toBe('diff --git a/x.py b/x.py\n@@')
    expect(result!.meta).toEqual({ is_error: false })
  })

  it('unwraps MCP content arrays with text blocks', () => {
    const envelope = {
      content: [{ type: 'text', text: 'hello\nworld' }],
      is_error: false,
    }
    const result = unwrapMcpResultEnvelope(envelope)
    expect(result).not.toBeNull()
    expect(result!.primary).toBe('hello\nworld')
    expect(result!.meta).toEqual({ is_error: false })
  })

  it('parses JSON strings and unwraps when they look like envelopes', () => {
    const stringified = JSON.stringify({ output: 'ok', session_id: 7 })
    const result = unwrapMcpResultEnvelope(stringified)
    expect(result).not.toBeNull()
    expect(result!.primary).toBe('ok')
    expect(result!.meta).toEqual({ session_id: 7 })
  })

  it('returns null for objects with no recognized primary field', () => {
    expect(unwrapMcpResultEnvelope({ status: 'ok', count: 3 })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// isReadOnlyBash
// ---------------------------------------------------------------------------
describe('isReadOnlyBash', () => {
  it('treats null/undefined/empty as not-read-only', () => {
    expect(isReadOnlyBash(null)).toBe(false)
    expect(isReadOnlyBash(undefined)).toBe(false)
    expect(isReadOnlyBash('')).toBe(false)
    expect(isReadOnlyBash('   ')).toBe(false)
  })

  it('matches single-word read verbs', () => {
    expect(isReadOnlyBash('ls -la')).toBe(true)
    expect(isReadOnlyBash('cat file.txt')).toBe(true)
    expect(isReadOnlyBash('grep pattern file')).toBe(true)
    expect(isReadOnlyBash('gcode outline foo.py')).toBe(true)
    expect(isReadOnlyBash('jq .')).toBe(true)
    expect(isReadOnlyBash('which gsqz')).toBe(true)
  })

  it('matches two-word read prefixes', () => {
    expect(isReadOnlyBash('git status')).toBe(true)
    expect(isReadOnlyBash('git diff src/foo.py')).toBe(true)
    expect(isReadOnlyBash('sed -n 1,120p file.py')).toBe(true)
    expect(isReadOnlyBash('gh pr view 42')).toBe(true)
  })

  it('treats mutating commands as not-read-only', () => {
    expect(isReadOnlyBash('git commit -m hi')).toBe(false)
    expect(isReadOnlyBash('rm -rf /tmp/foo')).toBe(false)
    expect(isReadOnlyBash('mv a b')).toBe(false)
    expect(isReadOnlyBash('cp a b')).toBe(false)
    expect(isReadOnlyBash('mkdir x')).toBe(false)
    expect(isReadOnlyBash('npm install foo')).toBe(false)
  })

  it('rejects shell redirects', () => {
    expect(isReadOnlyBash('cat file > out')).toBe(false)
    expect(isReadOnlyBash('echo hi >> log')).toBe(false)
  })

  it('accepts pipelines whose every segment is read-only', () => {
    expect(isReadOnlyBash('nl -ba foo.py | sed -n 1,120p')).toBe(true)
    expect(isReadOnlyBash('gcode search foo | jq .')).toBe(true)
  })

  it('rejects pipelines with any mutating segment', () => {
    expect(isReadOnlyBash('gcode search foo | xargs rm')).toBe(false)
    expect(isReadOnlyBash('cat foo && rm bar')).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// isReadOnlyMcp
// ---------------------------------------------------------------------------
describe('isReadOnlyMcp', () => {
  it('matches list_/get_/search_ prefixes on the tail of mcp__server__name', () => {
    expect(isReadOnlyMcp('mcp__gobby__list_tools')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__list_mcp_servers')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__get_skill')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__search_skills')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__read_mcp_resource')).toBe(true)
  })

  it('matches exact read-only MCP names', () => {
    expect(isReadOnlyMcp('mcp__gobby__outline')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__symbol')).toBe(true)
    expect(isReadOnlyMcp('mcp__gobby__usages')).toBe(true)
  })

  it('treats mutating MCP names as not-read-only', () => {
    expect(isReadOnlyMcp('mcp__gobby__set_variable')).toBe(false)
    expect(isReadOnlyMcp('mcp__gobby__add_mcp_server')).toBe(false)
    expect(isReadOnlyMcp('mcp__gobby__init_project')).toBe(false)
    expect(isReadOnlyMcp('mcp__gobby__remove_mcp_server')).toBe(false)
    expect(isReadOnlyMcp('mcp__gobby__import_mcp_server')).toBe(false)
    expect(isReadOnlyMcp('mcp__gobby__call_tool')).toBe(false)
  })

  it('treats null/empty as not-read-only', () => {
    expect(isReadOnlyMcp(null)).toBe(false)
    expect(isReadOnlyMcp(undefined)).toBe(false)
    expect(isReadOnlyMcp('')).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// defaultExpandedForCall
// ---------------------------------------------------------------------------
describe('defaultExpandedForCall', () => {
  it('expands pending_approval regardless of tool type', () => {
    const call = makeCall({
      id: '1',
      tool_name: 'Read',
      status: 'pending_approval',
    })
    expect(defaultExpandedForCall(call)).toBe(true)
  })

  it('expands edit/write tools', () => {
    expect(defaultExpandedForCall(makeCall({ id: '1', tool_name: 'Edit' }))).toBe(true)
    expect(defaultExpandedForCall(makeCall({ id: '2', tool_name: 'Write' }))).toBe(true)
    expect(defaultExpandedForCall(makeCall({ id: '3', tool_name: 'multiedit' }))).toBe(true)
  })

  it('collapses read/grep/glob/protocol tools', () => {
    expect(defaultExpandedForCall(makeCall({ id: '1', tool_name: 'Read' }))).toBe(false)
    expect(defaultExpandedForCall(makeCall({ id: '2', tool_name: 'Grep' }))).toBe(false)
    expect(defaultExpandedForCall(makeCall({ id: '3', tool_name: 'Glob' }))).toBe(false)
    expect(
      defaultExpandedForCall(makeCall({ id: '4', tool_name: 'protocol_context' })),
    ).toBe(false)
  })

  it('routes bash by command verb', () => {
    const readBash = makeCall({
      id: '1',
      tool_name: 'Bash',
      arguments: { command: 'gcode outline foo.py' },
    })
    expect(defaultExpandedForCall(readBash)).toBe(false)

    const writeBash = makeCall({
      id: '2',
      tool_name: 'Bash',
      arguments: { command: 'git commit -m wip' },
    })
    expect(defaultExpandedForCall(writeBash)).toBe(true)

    const noCmd = makeCall({ id: '3', tool_name: 'Bash', arguments: {} })
    expect(defaultExpandedForCall(noCmd)).toBe(true)
  })

  it('routes MCP by tool name', () => {
    const readMcp = makeCall({ id: '1', tool_name: 'mcp__gobby__list_tools' })
    expect(defaultExpandedForCall(readMcp)).toBe(false)

    const writeMcp = makeCall({ id: '2', tool_name: 'mcp__gobby__set_variable' })
    expect(defaultExpandedForCall(writeMcp)).toBe(true)
  })

  it('collapses unknown tool types so the header is sufficient', () => {
    const unknown = makeCall({ id: '1', tool_name: 'totally-not-a-thing', tool_type: '' })
    expect(defaultExpandedForCall(unknown)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// groupToolCalls — regression: only consecutive same-type runs group
// ---------------------------------------------------------------------------
describe('groupToolCalls regression', () => {
  it('groups consecutive same-tool runs of 3+ and keeps shorter runs flat', () => {
    // [Bash, Read, Read, Read, Bash, Bash, Bash, Read]
    // Threshold is 3: Read run (3) groups, Bash run (3) groups, single Bash + trailing Read stay flat.
    const calls = [
      makeCall({ id: '1', tool_name: 'Bash' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Read' }),
      makeCall({ id: '4', tool_name: 'Read' }),
      makeCall({ id: '5', tool_name: 'Bash' }),
      makeCall({ id: '6', tool_name: 'Bash' }),
      makeCall({ id: '7', tool_name: 'Bash' }),
      makeCall({ id: '8', tool_name: 'Read' }),
    ]
    const segments = groupToolCalls(calls)
    expect(segments).toHaveLength(4)

    expect(segments[0].kind).toBe('single')
    if (segments[0].kind === 'single') {
      expect(segments[0].call.id).toBe('1')
    }

    expect(segments[1].kind).toBe('group')
    if (segments[1].kind === 'group') {
      expect(segments[1].toolName).toBe('Read')
      expect(segments[1].tool_calls.map((c) => c.id)).toEqual(['2', '3', '4'])
    }

    expect(segments[2].kind).toBe('group')
    if (segments[2].kind === 'group') {
      expect(segments[2].toolName).toBe('Bash')
      expect(segments[2].tool_calls.map((c) => c.id)).toEqual(['5', '6', '7'])
    }

    expect(segments[3].kind).toBe('single')
    if (segments[3].kind === 'single') {
      expect(segments[3].call.id).toBe('8')
    }
  })

  it('never collapses calls with different tool_name into the same group', () => {
    const calls = [
      makeCall({ id: '1', tool_name: 'Bash' }),
      makeCall({ id: '2', tool_name: 'Read' }),
      makeCall({ id: '3', tool_name: 'Edit' }),
    ]
    const segments = groupToolCalls(calls)
    expect(segments).toHaveLength(3)
    expect(segments.every((s) => s.kind === 'single')).toBe(true)
  })
})
