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

function escapeRegex(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function buildTagPattern(tags: readonly string[]): string {
  return tags.map(escapeRegex).join('|')
}

const protocolToolTagPattern = buildTagPattern(PROTOCOL_TOOL_TAGS)
const inlineWrapperTagPattern = buildTagPattern(INLINE_WRAPPER_PROTOCOL_TAGS)

const protocolToolRe = new RegExp(
  `<(?<tag>${protocolToolTagPattern})(?=[\\s>])(?<attrs>[^>]*)>(?<body>[\\s\\S]*?)(?:<\\/\\k<tag>\\s*>|$)`,
  'gi',
)

const inlineWrapperProtocolTagRe = new RegExp(
  `</?(?:${inlineWrapperTagPattern})(?=[\\s>])[^>]*>`,
  'gi',
)

export type ProtocolContentSegment =
  | { type: 'text'; content: string }
  | { type: 'tool_call'; call: ToolCall }

function sanitizeVisibleProtocolText(content: string): string {
  if (!content.includes('<')) {
    return content
  }
  return content
    .replace(inlineWrapperProtocolTagRe, '')
    .replace(/\n{3,}/g, '\n\n')
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

function parseProtocolPayload(content: string): unknown {
  const trimmed = content.trim()
  if (!trimmed || !trimmed.includes('<')) {
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
    const value = parseProtocolPayload(match.groups.body)
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

export function splitProtocolContent(
  content: string,
  idPrefix: string,
): ProtocolContentSegment[] {
  if (!content.includes('<')) {
    return content ? [{ type: 'text', content }] : []
  }

  const segments: ProtocolContentSegment[] = []
  let lastIndex = 0
  let ordinal = 0

  for (const match of content.matchAll(protocolToolRe)) {
    if (!match.groups || match.index === undefined) {
      continue
    }

    const visibleText = sanitizeVisibleProtocolText(content.slice(lastIndex, match.index)).trimEnd()
    if (visibleText.trim()) {
      segments.push({ type: 'text', content: visibleText })
    }

    ordinal += 1
    segments.push({
      type: 'tool_call',
      call: makeProtocolToolCall(
        match.groups.tag,
        match.groups.body,
        match.groups.attrs,
        idPrefix,
        ordinal,
      ),
    })
    lastIndex = match.index + match[0].length
  }

  const trailingText = sanitizeVisibleProtocolText(content.slice(lastIndex)).trimStart()
  if (trailingText.trim()) {
    segments.push({ type: 'text', content: trailingText })
  }

  if (segments.length > 0) {
    return segments
  }

  const sanitized = sanitizeVisibleProtocolText(content)
  return sanitized.trim() ? [{ type: 'text', content: sanitized }] : []
}
