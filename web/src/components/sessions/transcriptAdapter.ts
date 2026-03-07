import type { ChatMessage, ToolCall } from '../../types/chat'
import type { SessionMessage } from '../../hooks/useSessionDetail'

function extractServerName(toolName: string): string {
  const parts = toolName.split('__')
  if (parts.length >= 3 && parts[0] === 'mcp') {
    return parts[1]
  }
  return ''
}

function tryParseJson(str: string | undefined): Record<string, unknown> | undefined {
  if (!str) return undefined
  try {
    const parsed = JSON.parse(str)
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

function tryParseResult(str: string | undefined): unknown {
  if (!str) return undefined
  try {
    return JSON.parse(str)
  } catch {
    return str
  }
}

/** Helper: append a tool call to the current tool_chain block, or start a new one. */
function appendToolBlock(msg: ChatMessage, tc: ToolCall) {
  if (!msg.contentBlocks) msg.contentBlocks = []
  const last = msg.contentBlocks[msg.contentBlocks.length - 1]
  if (last?.type === 'tool_chain') {
    last.calls.push(tc)
  } else {
    msg.contentBlocks.push({ type: 'tool_chain', calls: [tc] })
  }
}

/**
 * Transforms SessionMessage[] from the session detail API into ChatMessage[]
 * compatible with the chat page's MessageItem component.
 *
 * Tool-use messages are grouped into the preceding assistant message's toolCalls array
 * rather than rendered as standalone entries.
 */
export function sessionMessagesToChatMessages(messages: SessionMessage[]): ChatMessage[] {
  const result: ChatMessage[] = []
  let lastAssistant: ChatMessage | null = null

  for (const msg of messages) {
    const content = msg.content?.trim() ?? ''

    // Tool-use messages: create ToolCall and attach to last assistant message
    if (msg.tool_name) {
      const toolCall: ToolCall = {
        id: msg.tool_use_id || `tool-${msg.id}`,
        tool_name: msg.tool_name,
        server_name: extractServerName(msg.tool_name),
        status: 'completed',
        arguments: tryParseJson(msg.tool_input),
        result: tryParseResult(msg.tool_result),
      }
      if (lastAssistant) {
        lastAssistant.toolCalls = lastAssistant.toolCalls || []
        lastAssistant.toolCalls.push(toolCall)
        appendToolBlock(lastAssistant, toolCall)
      }
      continue
    }

    // Skip tool_result protocol messages (user-role messages containing tool_result JSON arrays)
    if (msg.role === 'user' && content.startsWith('[{') && content.includes('tool_result')) {
      continue
    }

    // Assistant messages that are JSON arrays of tool_use blocks
    if (msg.role === 'assistant' && content.startsWith('[{') && content.includes('tool_use')) {
      try {
        const calls = JSON.parse(content) as Array<{
          type?: string
          id?: string
          name?: string
          input?: unknown
        }>
        const tools = calls.filter((c) => c.type === 'tool_use')
        if (tools.length > 0) {
          const toolCalls = tools.map((t) => ({
            id: t.id || `tool-${msg.id}-${t.name}`,
            tool_name: t.name || 'unknown',
            server_name: extractServerName(t.name || ''),
            status: 'completed' as const,
            arguments:
              typeof t.input === 'object' && t.input !== null
                ? (t.input as Record<string, unknown>)
                : undefined,
          }))
          const chatMsg: ChatMessage = {
            id: String(msg.id),
            role: 'assistant',
            content: '',
            timestamp: new Date(msg.timestamp),
            toolCalls,
            contentBlocks: [{ type: 'tool_chain', calls: [...toolCalls] }],
          }
          lastAssistant = chatMsg
          result.push(chatMsg)
          continue
        }
      } catch {
        // Fall through to normal message handling
      }
    }

    // Skip empty messages
    if (!content && msg.role === 'assistant') continue
    if (!content) continue

    const role: ChatMessage['role'] =
      msg.role === 'user' ? 'user' : msg.role === 'assistant' ? 'assistant' : 'system'

    const chatMsg: ChatMessage = {
      id: String(msg.id),
      role,
      content,
      timestamp: new Date(msg.timestamp),
    }

    if (role === 'assistant') {
      chatMsg.contentBlocks = [{ type: 'text', content }]
      lastAssistant = chatMsg
    }

    result.push(chatMsg)
  }

  return result
}
