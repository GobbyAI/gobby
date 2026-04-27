import type { ToolCall } from '../../types/chat'

const PROTOCOL_TOOL_NAME = 'protocol_context'

const PROTOCOL_TOOL_TAGS = [
  'system-reminder',
  'task-notification',
  'local-command-caveat',
  'local-command-stdout',
  'command-name',
  'command-args',
  'command-message',
  'hook_context',
  'hook-context',
  'antml_thinking',
  'antml_function_calls',
  'antml_invoke',
  'environment_context',
  'skill',
  'permissions instructions',
  'permission instructions',
  'collaboration_mode',
  'turn_aborted',
  'system_instructions',
  'instructions',
  'skills_instructions',
] as const

const INLINE_WRAPPER_PROTOCOL_TAGS = [
  'proposed_plan',
  'proposed_implementation',
  'search_quality_reflection',
] as const

const PROTOCOL_CHILD_RE = /\s*<(?<tag>[\w:-]+)>(?<body>.*?)<\/\k<tag>\s*>/sy
const PROTOCOL_ATTR_RE = /([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g
const PROTOCOL_CODE_SPAN_RE = /```[\s\S]*?```|`[^`\n]*(?:`|$)/g
const MAX_PROTOCOL_PARSE_DEPTH = 10
const MAX_PROTOCOL_CONTENT_LENGTH = 200_000
const SYSTEM_BOOTSTRAP_PREFIX_RE =
  /^\s*(?:#\s*)?(?:AGENTS\.md instructions for\b|System instructions\b|Gobby Session ID:)/i
const SYSTEM_BOOTSTRAP_HEADING_RE = /^\s{0,3}(?:#{1,6}\s+)?([^:#]+):?\s*$/
const SYSTEM_BOOTSTRAP_HEADINGS = new Set([
  'platform context',
  'capabilities',
  'lifecycle model',
  'behavior',
  'role',
  'personality',
  'values',
  'interaction style',
  'general',
  'tools',
  'working with the user',
  'formatting rules',
  'final answer instructions',
  'intermediary updates',
])
const HIGH_SIGNAL_SYSTEM_BOOTSTRAP_HEADINGS = new Set([
  'platform context',
  'capabilities',
  'lifecycle model',
  'personality',
  'interaction style',
  'final answer instructions',
  'intermediary updates',
])

interface TextRange {
  start: number
  end: number
}

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function buildTagPattern(tags: readonly string[]): string {
  return tags.map(escapeRegex).join('|')
}

const protocolToolTagPattern = buildTagPattern(PROTOCOL_TOOL_TAGS)
const inlineWrapperTagPattern = buildTagPattern(INLINE_WRAPPER_PROTOCOL_TAGS)

const protocolTagRe = new RegExp(
  `<(?<closing>\\/)?(?<tag>${protocolToolTagPattern})(?=[\\s>])(?<attrs>[^>]*)>`,
  'gi',
)

const inlineWrapperProtocolTagRe = new RegExp(
  `</?(?:${inlineWrapperTagPattern})(?=[\\s>])[^>]*>`,
  'gi',
)

export type ProtocolContentSegment =
  | { type: 'text'; content: string }
  | { type: 'tool_call'; call: ToolCall }

interface ProtocolToolMatch {
  index: number
  end: number
  tag: string
  attrs: string
  body: string
}

function sanitizeVisibleProtocolText(content: string): string {
  if (!content.includes('<')) {
    return content
  }
  return content
    .replace(inlineWrapperProtocolTagRe, '')
    .replace(/\n{3,}/g, '\n\n')
}

function shouldParseProtocolContent(content: string): boolean {
  return (
    content.length <= MAX_PROTOCOL_CONTENT_LENGTH &&
    (content.includes('<') || looksLikeSystemBootstrapText(content))
  )
}

function countBootstrapHeadingMatches(content: string): [number, number] {
  const matchedHeadings = new Set<string>()
  const matchedHighSignalHeadings = new Set<string>()

  for (const rawLine of content.split('\n')) {
    const line = rawLine.trim()
    if (!line) {
      continue
    }

    const match = SYSTEM_BOOTSTRAP_HEADING_RE.exec(line)
    if (!match) {
      continue
    }

    const normalizedHeading = match[1].trim().toLowerCase()
    if (!SYSTEM_BOOTSTRAP_HEADINGS.has(normalizedHeading)) {
      continue
    }

    matchedHeadings.add(normalizedHeading)
    if (HIGH_SIGNAL_SYSTEM_BOOTSTRAP_HEADINGS.has(normalizedHeading)) {
      matchedHighSignalHeadings.add(normalizedHeading)
    }
  }

  return [matchedHeadings.size, matchedHighSignalHeadings.size]
}

function looksLikeSystemBootstrapText(content: string): boolean {
  const stripped = content.trim()
  if (!stripped) {
    return false
  }

  if (SYSTEM_BOOTSTRAP_PREFIX_RE.test(stripped)) {
    return true
  }

  const [headingCount, highSignalHeadingCount] = countBootstrapHeadingMatches(stripped)
  return headingCount >= 3 || (headingCount >= 2 && highSignalHeadingCount >= 1)
}

function findProtocolProtectedRanges(content: string): TextRange[] {
  const ranges: TextRange[] = []
  PROTOCOL_CODE_SPAN_RE.lastIndex = 0

  let match = PROTOCOL_CODE_SPAN_RE.exec(content)
  while (match) {
    ranges.push({ start: match.index, end: PROTOCOL_CODE_SPAN_RE.lastIndex })
    match = PROTOCOL_CODE_SPAN_RE.exec(content)
  }

  return ranges
}

function isProtectedProtocolIndex(index: number, ranges: TextRange[]): boolean {
  for (const range of ranges) {
    if (index < range.start) {
      return false
    }
    if (index < range.end) {
      return true
    }
  }

  return false
}

function parseProtocolAttributes(attrText: string): Record<string, string> | undefined {
  const attributes: Record<string, string> = {}
  for (const match of attrText.matchAll(PROTOCOL_ATTR_RE)) {
    const key = match[1]
    const value = match[2] ?? match[3] ?? ''
    attributes[key] = value
  }
  return Object.keys(attributes).length > 0 ? attributes : undefined
}

function parseProtocolPayload(content: string, depth = 0): unknown {
  const trimmed = content.trim()
  if (!trimmed || !trimmed.includes('<') || depth >= MAX_PROTOCOL_PARSE_DEPTH) {
    return trimmed
  }

  const parsedChildren: Record<string, unknown> = {}
  let matchedChild = false
  let index = 0

  while (index < trimmed.length) {
    PROTOCOL_CHILD_RE.lastIndex = index
    const match = PROTOCOL_CHILD_RE.exec(trimmed)
    if (!match?.groups) {
      break
    }

    matchedChild = true
    const tag = match.groups.tag
    const value = parseProtocolPayload(match.groups.body, depth + 1)
    const existing = parsedChildren[tag]
    if (existing === undefined) {
      parsedChildren[tag] = value
    } else if (Array.isArray(existing)) {
      existing.push(value)
    } else {
      parsedChildren[tag] = [existing, value]
    }
    index = PROTOCOL_CHILD_RE.lastIndex
  }

  if (matchedChild && !trimmed.slice(index).trim()) {
    return parsedChildren
  }

  return trimmed
}

function findMatchingProtocolClose(
  content: string,
  startIndex: number,
  normalizedTag: string,
  protectedRanges: TextRange[],
): RegExpExecArray | null {
  let depth = 1
  protocolTagRe.lastIndex = startIndex

  let match = protocolTagRe.exec(content)
  while (match) {
    if (!match.groups) {
      match = protocolTagRe.exec(content)
      continue
    }
    if (isProtectedProtocolIndex(match.index, protectedRanges)) {
      match = protocolTagRe.exec(content)
      continue
    }
    if (match.groups.tag.toLowerCase() !== normalizedTag) {
      match = protocolTagRe.exec(content)
      continue
    }

    if (match.groups.closing) {
      depth -= 1
      if (depth === 0) {
        return match
      }
    } else {
      depth += 1
    }
    match = protocolTagRe.exec(content)
  }

  return null
}

function findProtocolToolMatches(content: string): ProtocolToolMatch[] {
  const matches: ProtocolToolMatch[] = []
  const protectedRanges = findProtocolProtectedRanges(content)
  protocolTagRe.lastIndex = 0

  let match = protocolTagRe.exec(content)
  while (match) {
    if (
      !match.groups ||
      match.groups.closing ||
      match.index === undefined ||
      isProtectedProtocolIndex(match.index, protectedRanges)
    ) {
      match = protocolTagRe.exec(content)
      continue
    }

    const openEnd = match.index + match[0].length
    const normalizedTag = match.groups.tag.toLowerCase()
    const closingMatch = findMatchingProtocolClose(
      content,
      openEnd,
      normalizedTag,
      protectedRanges,
    )
    if (!closingMatch?.groups || closingMatch.index === undefined) {
      protocolTagRe.lastIndex = openEnd
      match = protocolTagRe.exec(content)
      continue
    }

    const end = closingMatch.index + closingMatch[0].length
    matches.push({
      index: match.index,
      end,
      tag: match.groups.tag,
      attrs: match.groups.attrs,
      body: content.slice(openEnd, closingMatch.index),
    })
    protocolTagRe.lastIndex = end
    match = protocolTagRe.exec(content)
  }

  return matches
}

function makeProtocolToolCall(
  tag: string,
  body: string,
  attrs: string,
  idPrefix: string,
  ordinal: number,
): ToolCall {
  const normalizedTag = tag.toLowerCase()
  const attributes = parseProtocolAttributes(attrs)
  const resultContent = parseProtocolPayload(body)

  return {
    id: `${idPrefix}-protocol-${ordinal}`,
    tool_name: PROTOCOL_TOOL_NAME,
    server_name: 'builtin',
    tool_type: 'protocol',
    status: 'completed',
    arguments: attributes ? { tag: normalizedTag, attributes } : { tag: normalizedTag },
    result: {
      content: resultContent,
      content_type: typeof resultContent === 'object' && resultContent !== null ? 'json' : 'text',
      truncated: false,
      metadata: { protocol_tag: normalizedTag },
    },
  }
}

function appendVisibleProtocolContent(
  segments: ProtocolContentSegment[],
  content: string,
  idPrefix: string,
  ordinal: number,
): number {
  if (!content.trim()) {
    return ordinal
  }

  if (looksLikeSystemBootstrapText(content)) {
    const nextOrdinal = ordinal + 1
    segments.push({
      type: 'tool_call',
      call: makeProtocolToolCall(
        'system_instructions',
        content.trim(),
        '',
        idPrefix,
        nextOrdinal,
      ),
    })
    return nextOrdinal
  }

  segments.push({ type: 'text', content })
  return ordinal
}

export function splitProtocolContent(
  content: string,
  idPrefix: string,
): ProtocolContentSegment[] {
  if (!shouldParseProtocolContent(content)) {
    return content ? [{ type: 'text', content }] : []
  }

  const segments: ProtocolContentSegment[] = []
  let lastIndex = 0
  let ordinal = 0

  for (const match of findProtocolToolMatches(content)) {
    const visibleText = sanitizeVisibleProtocolText(content.slice(lastIndex, match.index)).trimEnd()
    ordinal = appendVisibleProtocolContent(segments, visibleText, idPrefix, ordinal)

    ordinal += 1
    segments.push({
      type: 'tool_call',
      call: makeProtocolToolCall(
        match.tag,
        match.body,
        match.attrs,
        idPrefix,
        ordinal,
      ),
    })
    lastIndex = match.end
  }

  const trailingText = sanitizeVisibleProtocolText(content.slice(lastIndex)).trimStart()
  appendVisibleProtocolContent(segments, trailingText, idPrefix, ordinal)

  if (segments.length > 0) {
    return segments
  }

  const sanitized = sanitizeVisibleProtocolText(content)
  const fallbackSegments: ProtocolContentSegment[] = []
  appendVisibleProtocolContent(fallbackSegments, sanitized, idPrefix, 0)
  return fallbackSegments
}

export function hasProtocolToolContent(content: string): boolean {
  if (!shouldParseProtocolContent(content)) {
    return false
  }

  return findProtocolToolMatches(content).length > 0 || looksLikeSystemBootstrapText(content)
}
