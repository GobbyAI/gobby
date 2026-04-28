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

function fallbackUuid(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    crypto.getRandomValues(bytes)
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0'))
    return [
      hex.slice(0, 4).join(''),
      hex.slice(4, 6).join(''),
      hex.slice(6, 8).join(''),
      hex.slice(8, 10).join(''),
      hex.slice(10, 16).join(''),
    ].join('-')
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

function createFallbackMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `ws-${crypto.randomUUID()}`
  }
  return `ws-${fallbackUuid()}`
}

function isHookFeedback(content: string): boolean {
  return (
    /^Stop hook feedback:/.test(content) ||
    /^(Pre|Post)ToolUse hook/.test(content) ||
    /^UserPromptSubmit hook/.test(content)
  )
}

const SYSTEM_BOOTSTRAP_PREFIX_RE =
  /^\s*(?:#\s*)?(?:AGENTS\.md instructions for\b|System(?: instructions|:)|Gobby Session ID:)/i

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

  return SYSTEM_BOOTSTRAP_PREFIX_RE.test(stripped)
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
