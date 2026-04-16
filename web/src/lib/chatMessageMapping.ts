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

function isHookFeedback(content: string): boolean {
  return (
    /^Stop hook feedback:/.test(content) ||
    /^(Pre|Post)ToolUse hook/.test(content) ||
    /^UserPromptSubmit hook/.test(content)
  )
}

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
    id: String(message.id ?? `ws-${Date.now()}`),
    role: normalizeChatRole(message.role, message.content, contentBlocks),
    content: typeof message.content === 'string' ? message.content : '',
    timestamp: new Date((message.timestamp as string | Date | undefined) ?? Date.now()),
    contentBlocks,
  }

  if (chatMsg.contentBlocks) {
    for (const block of chatMsg.contentBlocks) {
      if (block.type === 'tool_chain' && block.tool_calls) {
        chatMsg.toolCalls = [...(chatMsg.toolCalls || []), ...block.tool_calls]
      } else if (block.type === 'thinking') {
        chatMsg.thinkingContent = (chatMsg.thinkingContent || '') + block.content
      }
    }
  }

  return chatMsg
}
