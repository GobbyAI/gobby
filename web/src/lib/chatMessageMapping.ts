import type { ChatMessage, ContentBlock, ToolCall } from '../types/chat'
import { hasProtocolToolContent } from '../components/chat/protocolContent'

type ChatRole = ChatMessage['role']

type RenderedMessageLike = {
  id?: string
  role?: unknown
  content?: unknown
  timestamp?: string | Date
  content_blocks?: ContentBlock[]
}

let fallbackMessageIdCounter = 0

function createFallbackMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `ws-${crypto.randomUUID()}`
  }
  // Counter fallback is only used when randomUUID is unavailable.
  fallbackMessageIdCounter += 1
  return `ws-${fallbackMessageIdCounter}`
}

function isHookFeedback(content: string): boolean {
  return (
    /^Stop hook feedback:/.test(content) ||
    /^(Pre|Post)ToolUse hook/.test(content) ||
    /^UserPromptSubmit hook/.test(content)
  )
}

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

function isProtocolToolCall(toolCall: ToolCall): boolean {
  return toolCall.tool_type === 'protocol' ||
    toolCall.tool_name === 'protocol_context' ||
    toolCall.tool_name === 'protocol'
}

function hasProtocolOnlyContentBlocks(contentBlocks: ContentBlock[] | undefined): boolean {
  if (!contentBlocks || contentBlocks.length === 0) {
    return false
  }

  let sawProtocolToolCall = false

  for (const block of contentBlocks) {
    if (block.type === 'text') {
      if (block.content.trim()) {
        return false
      }
      continue
    }

    if (block.type !== 'tool_chain') {
      return false
    }

    if (block.tool_calls.length === 0 || !block.tool_calls.every(isProtocolToolCall)) {
      return false
    }

    sawProtocolToolCall = true
  }

  return sawProtocolToolCall
}

export function looksLikeSystemBootstrapText(content: string): boolean {
  const stripped = content.trim()
  if (!stripped) {
    return false
  }

  if (SYSTEM_BOOTSTRAP_PREFIX_RE.test(stripped)) {
    return true
  }

  const matchedHeadings = new Set<string>()
  const matchedHighSignalHeadings = new Set<string>()

  for (const rawLine of stripped.split('\n')) {
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

  return matchedHeadings.size >= 3 || (
    matchedHeadings.size >= 2 &&
    matchedHighSignalHeadings.size >= 1
  )
}

export function normalizeChatRole(
  role: unknown,
  content: unknown,
  contentBlocks?: ContentBlock[],
): ChatRole {
  const normalizedRole: ChatRole =
    role === 'user' || role === 'assistant' || role === 'system'
      ? role
      : 'assistant'

  if (normalizedRole !== 'user') {
    return normalizedRole
  }

  const textContent = typeof content === 'string' ? content : ''
  if (
    isHookFeedback(textContent) ||
    looksLikeSystemBootstrapText(textContent) ||
    hasProtocolToolContent(textContent) ||
    hasProtocolOnlyContentBlocks(contentBlocks)
  ) {
    return 'system'
  }

  return normalizedRole
}

export function mapRenderedMessageToChatMessage(
  message: RenderedMessageLike,
): ChatMessage {
  const contentBlocks = message.content_blocks
  const chatMsg: ChatMessage = {
    id: message.id == null ? createFallbackMessageId() : String(message.id),
    role: normalizeChatRole(message.role, message.content, contentBlocks),
    content: typeof message.content === 'string' ? message.content : '',
    timestamp: new Date((message.timestamp as string | Date | undefined) ?? Date.now()),
    contentBlocks,
  }

  const thinkingParts: string[] = []
  if (chatMsg.contentBlocks) {
    for (const block of chatMsg.contentBlocks) {
      if (block.type === 'tool_chain' && block.tool_calls?.length) {
        if (!chatMsg.toolCalls) {
          chatMsg.toolCalls = []
        }
        chatMsg.toolCalls.push(...block.tool_calls)
      } else if (block.type === 'thinking') {
        thinkingParts.push(block.content)
      }
    }
  }
  if (thinkingParts.length > 0) {
    chatMsg.thinkingContent = thinkingParts.join('')
  }

  return chatMsg
}
