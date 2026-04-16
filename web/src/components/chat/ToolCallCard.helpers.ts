import type { ToolCall, ToolResult } from '../../types/chat'
import { classifyTool } from '../../types/chat'

const FILE_TOOL_TYPES = new Set(['read', 'edit'])
const COMPACT_HEADER_TOOL_TYPES = new Set(['read', 'bash', 'grep', 'glob', 'protocol'])
const COMPACT_HEADER_NAMES = new Set(['list_mcp_servers', 'ExitPlanMode'])
const UNGROUPABLE_TOOLS = new Set(['render_surface', 'AskUserQuestion'])
const SHELL_ALIAS_NAMES = new Set([
  'bash',
  'shell',
  'run_command',
  'run_shell_command',
  'runshellcommand',
  'shelltool',
  'commandexecution',
  'exec_command',
])
const EXT_TO_LANGUAGE: Record<string, string> = {
  py: 'python', tsx: 'tsx', ts: 'typescript', jsx: 'jsx', js: 'javascript',
  json: 'json', yaml: 'yaml', yml: 'yaml', md: 'markdown', css: 'css',
  html: 'html', sh: 'bash', bash: 'bash', zsh: 'bash', sql: 'sql',
  rs: 'rust', go: 'go', rb: 'ruby', java: 'java', c: 'c', cpp: 'cpp',
  h: 'c', hpp: 'cpp', toml: 'toml', xml: 'xml', svg: 'xml',
}
const DATA_URI_RE = /^data:image\/(png|jpe?g|gif|webp|svg\+xml);base64,/

export function formatToolName(fullName: string): string {
  const parts = fullName.split('__')
  return parts[parts.length - 1] || fullName
}

export function truncStr(str: string | undefined | null, max: number): string | null {
  if (!str) return null
  return str.length > max ? `${str.slice(0, max - 1)}\u2026` : str
}

export function pathBasename(path: string): string {
  const parts = path.split('/')
  return parts[parts.length - 1] || path
}

export function resolveToolType(call: ToolCall): string {
  if (call.tool_type) return call.tool_type
  return classifyTool(formatToolName(call.tool_name))
}

function getShellCommand(args: Record<string, unknown>): string | null {
  const command = args.command
  if (typeof command === 'string' && command.trim()) return command

  const cmd = args.cmd
  if (typeof cmd === 'string' && cmd.trim()) return cmd

  return null
}

export function getToolDisplayName(call: ToolCall): string {
  const name = formatToolName(call.tool_name)
  if (resolveToolType(call) === 'protocol') {
    return 'Protocol'
  }
  if (resolveToolType(call) === 'bash' && SHELL_ALIAS_NAMES.has(name.toLowerCase())) {
    return 'Bash'
  }
  return name
}

function isTypedResult(result: unknown): result is ToolResult {
  if (typeof result !== 'object' || result === null) return false
  const obj = result as Record<string, unknown>
  return 'content' in obj && 'content_type' in obj
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function tryParseJsonValue(value: string): unknown {
  const trimmed = value.trim()
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) return value

  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

function normalizeDisplayResult(
  content: unknown,
  metadata?: Record<string, unknown>,
): { content: unknown; metadata?: Record<string, unknown> } {
  const parsedContent = typeof content === 'string' ? tryParseJsonValue(content) : content
  if (
    !isRecord(parsedContent) ||
    typeof parsedContent.success !== 'boolean' ||
    typeof parsedContent.response_time_ms !== 'number'
  ) {
    return { content, metadata }
  }

  const mergedMetadata = { ...(metadata ?? {}) }
  if (mergedMetadata.response_time_ms == null) {
    mergedMetadata.response_time_ms = parsedContent.response_time_ms
  }

  const innerContent = 'result' in parsedContent
    ? parsedContent.result
    : {
        success: parsedContent.success,
        error: parsedContent.error,
      }

  if (isRecord(innerContent) && mergedMetadata.response_time_ms != null) {
    return {
      content: innerContent.response_time_ms == null
        ? { ...innerContent, response_time_ms: mergedMetadata.response_time_ms }
        : innerContent,
      metadata: mergedMetadata,
    }
  }

  return {
    content: innerContent,
    metadata: mergedMetadata,
  }
}

export function extractResultContent(result: unknown): unknown {
  if (!isTypedResult(result)) return normalizeDisplayResult(result).content
  return normalizeDisplayResult(result.content, result.metadata).content
}

export function extractResultMetadata(
  result: unknown,
): Record<string, unknown> | undefined {
  if (!isTypedResult(result)) return normalizeDisplayResult(result).metadata
  return normalizeDisplayResult(result.content, result.metadata).metadata
}

export function getToolSummary(call: ToolCall): string | null {
  const args = call.arguments || {}
  const name = formatToolName(call.tool_name)
  const toolType = resolveToolType(call)

  switch (toolType) {
    case 'read':
    case 'edit':
      return (args.file_path as string) || null
    case 'bash':
      return truncStr(getShellCommand(args), 80)
    case 'protocol':
      return (args.tag as string) || null
    case 'grep': {
      const pattern = args.pattern as string
      const path = args.path as string
      if (!pattern) return null
      return path ? `"${pattern}" in ${path}` : `"${pattern}"`
    }
    case 'glob':
      return (args.pattern as string) || null
  }

  switch (name) {
    case 'Task': {
      const agentType = args.subagent_type as string
      const desc = args.description as string
      if (!agentType) return null
      return desc ? `${agentType} (${truncStr(desc, 40)})` : agentType
    }
    case 'WebFetch':
      return truncStr(args.url as string, 60)
    case 'WebSearch':
      return args.query ? `"${truncStr(args.query as string, 60)}"` : null
    case 'list_mcp_servers':
    case 'ExitPlanMode':
      return null
    case 'list_tools':
      return (args.server_name as string) || null
    case 'get_tool_schema':
    case 'call_tool': {
      const server = args.server_name as string
      const tool = args.tool_name as string
      return server && tool ? `${server}.${tool}` : null
    }
    case 'recommend_tools':
      return args.task_description
        ? `"${truncStr(args.task_description as string, 60)}"`
        : null
    case 'search_tools':
      return args.query ? `"${truncStr(args.query as string, 60)}"` : null
    case 'Agent': {
      const desc = args.description as string
      const agentType = args.subagent_type as string
      const parts = [agentType, desc ? truncStr(desc, 50) : null].filter(Boolean)
      return parts.length > 0 ? parts.join(': ') : null
    }
    default:
      if (
        call.server_name &&
        call.server_name !== 'builtin' &&
        call.server_name !== 'unknown'
      ) {
        return `${call.server_name}.${name}`
      }
      return null
  }
}

export interface ToolCallGroup {
  kind: 'group'
  toolName: string
  displayName: string
  tool_calls: ToolCall[]
  hasErrors: boolean
  allCompleted: boolean
  hasInFlight: boolean
}

export interface ToolCallSingle {
  kind: 'single'
  call: ToolCall
}

export type ToolCallSegment = ToolCallGroup | ToolCallSingle

export function groupToolCalls(toolCalls: ToolCall[]): ToolCallSegment[] {
  const segments: ToolCallSegment[] = []
  let i = 0

  while (i < toolCalls.length) {
    const call = toolCalls[i]

    if (UNGROUPABLE_TOOLS.has(call.tool_name) || call.status === 'pending_approval') {
      segments.push({ kind: 'single', call })
      i++
      continue
    }

    let j = i + 1
    while (
      j < toolCalls.length &&
      toolCalls[j].tool_name === call.tool_name &&
      !UNGROUPABLE_TOOLS.has(toolCalls[j].tool_name) &&
      toolCalls[j].status !== 'pending_approval'
    ) {
      j++
    }

    if (j - i >= 2) {
      const calls = toolCalls.slice(i, j)
      segments.push({
        kind: 'group',
        toolName: call.tool_name,
        displayName: getToolDisplayName(call),
        tool_calls: calls,
        hasErrors: calls.some((entry) => entry.status === 'error'),
        allCompleted: calls.every((entry) => entry.status === 'completed'),
        hasInFlight: calls.some((entry) => entry.status === 'calling'),
      })
    } else {
      segments.push({ kind: 'single', call })
    }

    i = j
  }

  return segments
}

export function parseReadOutput(
  result: string,
): { content: string; startLine: number } | null {
  const lines = result.split('\n')
  const parsed: string[] = []
  let startLine = 1
  let firstLine = true

  for (const line of lines) {
    const match = line.match(/^\s*(\d+)\u2192(.*)$/)
    if (!match) {
      if (line.trim() === '') {
        parsed.push('')
        continue
      }
      return null
    }
    if (firstLine) {
      startLine = parseInt(match[1], 10)
      firstLine = false
    }
    parsed.push(match[2])
  }

  if (parsed.length === 0) return null
  return { content: parsed.join('\n').replace(/\n$/, ''), startLine }
}

export interface GrepFileGroup {
  filePath: string
  lines: { lineNum: number; content: string }[]
}

export function parseGrepOutput(result: string): GrepFileGroup[] | null {
  const lines = result.split('\n')
  const groups: GrepFileGroup[] = []
  let currentGroup: GrepFileGroup | null = null
  let matchCount = 0

  for (const line of lines) {
    if (line === '--' || line === '') {
      currentGroup = null
      continue
    }
    const match = line.match(/^(.+?):(\d+)[:-](.*)$/)
    if (!match) {
      if (matchCount === 0) return null
      continue
    }
    const [, fp, lineNumStr, content] = match
    const lineNum = parseInt(lineNumStr, 10)
    matchCount++
    if (!currentGroup || currentGroup.filePath !== fp) {
      currentGroup = { filePath: fp, lines: [] }
      groups.push(currentGroup)
    }
    currentGroup.lines.push({ lineNum, content })
  }

  return groups.length > 0 ? groups : null
}

export function getLanguageFromPath(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase() || ''
  return EXT_TO_LANGUAGE[ext] || 'text'
}

export function computeLineDiff(
  oldStr: string,
  newStr: string,
): { type: 'keep' | 'add' | 'remove'; line: string }[] {
  const oldLines = oldStr.split('\n')
  const newLines = newStr.split('\n')
  const n = oldLines.length
  const m = newLines.length

  if (n + m > 500) {
    return [
      ...oldLines.map((line) => ({ type: 'remove' as const, line })),
      ...newLines.map((line) => ({ type: 'add' as const, line })),
    ]
  }

  const dp: number[][] = Array.from({ length: n + 1 }, () =>
    Array(m + 1).fill(0),
  )
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      dp[i][j] =
        oldLines[i - 1] === newLines[j - 1]
          ? dp[i - 1][j - 1] + 1
          : Math.max(dp[i - 1][j], dp[i][j - 1])
    }
  }

  const result: { type: 'keep' | 'add' | 'remove'; line: string }[] = []
  let i = n
  let j = m

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.push({ type: 'keep', line: oldLines[i - 1] })
      i--
      j--
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.push({ type: 'add', line: newLines[j - 1] })
      j--
    } else {
      result.push({ type: 'remove', line: oldLines[i - 1] })
      i--
    }
  }

  result.reverse()
  return result
}

export function extractBase64Image(result: unknown): string | null {
  if (typeof result === 'string' && DATA_URI_RE.test(result)) return result
  if (typeof result !== 'object' || result === null) return null

  const obj = result as Record<string, unknown>
  if (obj.type === 'image' && typeof obj.source === 'object' && obj.source !== null) {
    const src = obj.source as Record<string, unknown>
    if (
      src.type === 'base64' &&
      typeof src.data === 'string' &&
      typeof src.media_type === 'string'
    ) {
      return `data:${src.media_type};base64,${src.data}`
    }
  }

  if (Array.isArray(result)) {
    for (const item of result) {
      const found = extractBase64Image(item)
      if (found) return found
    }
  }

  return null
}

export function buildChainSummary(toolCalls: ToolCall[]): string {
  const counts = new Map<string, number>()
  for (const toolCall of toolCalls) {
    const name = getToolDisplayName(toolCall)
    counts.set(name, (counts.get(name) || 0) + 1)
  }

  return Array.from(counts.entries())
    .map(([name, count]) => (count > 1 ? `${count} ${name}` : name))
    .join(', ')
}

export { COMPACT_HEADER_NAMES, COMPACT_HEADER_TOOL_TYPES, FILE_TOOL_TYPES }
