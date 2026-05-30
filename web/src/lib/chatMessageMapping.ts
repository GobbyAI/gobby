import type { ChatMessage, ContentBlock, TokenUsage, ToolCall, ToolResult } from '../types/chat'
import { classifyTool } from '../types/chat'
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

export function isHookFeedback(content: string): boolean {
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

export interface ApiMessage {
  id?: string
  role: string
  content: string
  content_type?: string
  tool_name?: string
  tool_input?: string
  tool_result?: string
  tool_use_id?: string
  timestamp: string
  message_index?: number
  content_blocks?: ContentBlock[]
  model?: string | null
  usage?: TokenUsage | null
}

export function mapStoredChatMessage(m: {
  id: string
  role: string
  content: string
  tool_calls?: ToolCall[]
  content_blocks?: ContentBlock[]
  created_at: string
}): ChatMessage {
  if (m.content_blocks?.length) {
    return mapRenderedMessageToChatMessage({
      id: m.id,
      role: m.role,
      content: m.content,
      timestamp: m.created_at,
      content_blocks: m.content_blocks,
    })
  }

  return {
    id: m.id,
    role: normalizeChatRole(m.role, m.content),
    content: m.content,
    contentBlocks: m.content ? [{ type: 'text' as const, content: m.content }] : [],
    toolCalls: m.tool_calls ?? [],
    timestamp: new Date(m.created_at),
  }
}

export function tryParseJSON(value: unknown): unknown {
  if (value === undefined || value === null) return undefined
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

export function appendTextBlock(msg: ChatMessage, text: string) {
  if (!msg.contentBlocks) msg.contentBlocks = []
  const last = msg.contentBlocks[msg.contentBlocks.length - 1]
  if (last?.type === 'text') {
    if (last.content && !last.content.endsWith('\n')) last.content += '\n'
    last.content += text
  } else {
    msg.contentBlocks.push({ type: 'text', content: text })
  }
}

export function appendToolBlock(msg: ChatMessage, tc: ToolCall) {
  if (!msg.contentBlocks) msg.contentBlocks = []
  msg.contentBlocks.push({ type: 'tool_chain', tool_calls: [tc] })
}

export function findToolCallById(
  msg: ChatMessage,
  toolUseId: string,
): ToolCall | undefined {
  if (msg.contentBlocks) {
    for (const block of msg.contentBlocks) {
      if (block.type === 'tool_chain') {
        const found = block.tool_calls.find((tc) => tc.id === toolUseId)
        if (found) return found
      }
    }
  }
  return msg.toolCalls?.find((tc) => tc.id === toolUseId)
}

export function findPendingToolCall(msg: ChatMessage): ToolCall | undefined {
  if (msg.contentBlocks) {
    for (let i = msg.contentBlocks.length - 1; i >= 0; i--) {
      const block = msg.contentBlocks[i]
      if (block.type === 'tool_chain') {
        const pending = block.tool_calls.find(
          (tc) => tc.status !== 'completed',
        )
        if (pending) return pending
      }
    }
  }
  return msg.toolCalls?.find((tc) => tc.status !== 'completed')
}

export function extractServerName(toolName: string): string {
  const parts = toolName.split('__')
  if (parts.length >= 3 && parts[0] === 'mcp') return parts[1]
  return 'builtin'
}

export function extractUserText(content: string): string | null {
  if (!content.startsWith('[') || !content.endsWith(']')) return null
  let blocks: Array<{ type?: string; text?: string; content?: string }> | null =
    null
  try {
    const parsed = JSON.parse(content)
    if (Array.isArray(parsed)) blocks = parsed
  } catch {
    return null
  }
  if (!blocks || blocks.length === 0) return null
  const texts: string[] = []
  for (const block of blocks) {
    const text = block.text ?? block.content ?? ''
    if (!text) continue
    if (text.includes('<hook_context>') || text.includes('</hook_context>')) {
      continue
    }
    if (
      text.includes('<system-reminder>') ||
      text.includes('</system-reminder>')
    ) {
      continue
    }
    if (
      text.includes('<system_instructions>') ||
      text.includes('</system_instructions>')
    ) {
      continue
    }
    texts.push(text)
  }
  return texts.length > 0 ? texts.join('\n\n') : ''
}

type ApiMappingState = {
  result: ChatMessage[]
  currentAssistant: ChatMessage | null
}

function flushAssistant(state: ApiMappingState) {
  if (state.currentAssistant) {
    state.result.push(state.currentAssistant)
    state.currentAssistant = null
  }
}

function ensureAssistant(
  state: ApiMappingState,
  id: string,
  timestamp: Date,
  initial?: Partial<ChatMessage>,
): ChatMessage {
  if (!state.currentAssistant) {
    state.currentAssistant = {
      id,
      role: 'assistant',
      content: '',
      timestamp,
      ...initial,
    }
  }
  return state.currentAssistant
}

function appendAssistantText(
  state: ApiMappingState,
  id: string,
  timestamp: Date,
  text: string,
) {
  if (state.currentAssistant) {
    if (text) {
      if (
        state.currentAssistant.content &&
        !state.currentAssistant.content.endsWith('\n')
      ) {
        state.currentAssistant.content += '\n'
      }
      state.currentAssistant.content += text
      appendTextBlock(state.currentAssistant, text)
    }
    return
  }

  state.currentAssistant = {
    id,
    role: 'assistant',
    content: text || '',
    timestamp,
    contentBlocks: text ? [{ type: 'text', content: text }] : [],
  }
}

function completeToolCallFromMessage(assistant: ChatMessage, message: ApiMessage) {
  const match = message.tool_use_id
    ? findToolCallById(assistant, message.tool_use_id)
    : findPendingToolCall(assistant)
  if (match) {
    match.result = tryParseJSON(message.content) as ToolResult | undefined
    match.status = 'completed'
  }
}

function markLatestToolCallError(assistant: ChatMessage, content: string) {
  if (!assistant.toolCalls?.length) return false

  const lastTc = assistant.toolCalls[assistant.toolCalls.length - 1]
  lastTc.error = content
  lastTc.status = 'error'
  if (assistant.contentBlocks) {
    for (const block of assistant.contentBlocks) {
      if (block.type === 'tool_chain') {
        const tcMatch = block.tool_calls.find((toolCall) => toolCall.id === lastTc.id)
        if (tcMatch) {
          tcMatch.error = content
          tcMatch.status = 'error'
        }
      }
    }
  }
  return true
}

function mapContentBlockMessage(
  state: ApiMappingState,
  message: ApiMessage,
  id: string,
  timestamp: Date,
) {
  flushAssistant(state)

  const chatMsg: ChatMessage = {
    id,
    role: normalizeChatRole(message.role, message.content, message.content_blocks),
    content: message.content || '',
    timestamp,
    contentBlocks: message.content_blocks,
  }

  for (const block of message.content_blocks || []) {
    if (block.type === 'tool_chain' && block.tool_calls) {
      chatMsg.toolCalls = [...(chatMsg.toolCalls || []), ...block.tool_calls]
    } else if (block.type === 'thinking') {
      chatMsg.thinkingContent = (chatMsg.thinkingContent || '') + block.content
    }
  }

  state.result.push(chatMsg)
}

function mapUserApiMessage(
  state: ApiMappingState,
  message: ApiMessage,
  id: string,
  timestamp: Date,
  content: string,
) {
  if (message.content_type === 'tool_result' || message.tool_use_id) {
    if (state.currentAssistant) {
      completeToolCallFromMessage(state.currentAssistant, message)
    }
    return
  }

  if (content.startsWith('[{') && content.includes('tool_result')) return

  if (isHookFeedback(content)) {
    if (
      state.currentAssistant &&
      markLatestToolCallError(state.currentAssistant, content)
    ) {
      return
    }
    flushAssistant(state)
    state.result.push({ id, role: 'system', content, timestamp })
    return
  }

  if (content.startsWith('[')) {
    const extracted = extractUserText(content)
    if (extracted !== null) {
      if (!extracted.trim()) return
      flushAssistant(state)
      state.result.push({ id, role: 'user', content: extracted, timestamp })
      return
    }
  }

  flushAssistant(state)
  state.result.push({ id, role: 'user', content: message.content || '', timestamp })
}

function toolCallFromApiMessage(message: ApiMessage, id: string): ToolCall {
  const toolName = message.tool_name || 'unknown'
  return {
    id: message.tool_use_id || id,
    tool_name: toolName,
    server_name: extractServerName(toolName),
    tool_type: classifyTool(toolName),
    status: message.tool_result ? 'completed' : 'calling',
    arguments: tryParseJSON(message.tool_input) as Record<string, unknown> | undefined,
    result: message.tool_result
      ? (tryParseJSON(message.tool_result) as ToolResult)
      : undefined,
  }
}

function protocolToolCallsFromContent(content: string, id: string): ToolCall[] | null {
  try {
    const calls = JSON.parse(content) as Array<{
      type?: string
      id?: string
      name?: string
      input?: unknown
    }>
    const tools = calls.filter((call) => call.type === 'tool_use')
    if (tools.length === 0) return null

    return tools.map((tool) => {
      const toolName = tool.name || 'unknown'
      return {
        id: tool.id || `tool-${id}-${toolName}`,
        tool_name: toolName,
        server_name: extractServerName(toolName),
        tool_type: classifyTool(toolName),
        status: 'completed' as const,
        arguments:
          typeof tool.input === 'object' && tool.input !== null
            ? (tool.input as Record<string, unknown>)
            : undefined,
      }
    })
  } catch {
    return null
  }
}

function appendProtocolToolCalls(
  state: ApiMappingState,
  id: string,
  timestamp: Date,
  toolCalls: ToolCall[],
) {
  if (!state.currentAssistant) {
    state.currentAssistant = {
      id,
      role: 'assistant',
      content: '',
      timestamp,
      toolCalls,
      contentBlocks: [{ type: 'tool_chain', tool_calls: [...toolCalls] }],
    }
    return
  }

  state.currentAssistant.toolCalls = [
    ...(state.currentAssistant.toolCalls || []),
    ...toolCalls,
  ]
  for (const toolCall of toolCalls) appendToolBlock(state.currentAssistant, toolCall)
}

function mapAssistantApiMessage(
  state: ApiMappingState,
  message: ApiMessage,
  id: string,
  timestamp: Date,
  content: string,
) {
  if (message.content_type === 'tool_use' || message.tool_name) {
    const assistant = ensureAssistant(state, id, timestamp, {
      toolCalls: [],
      contentBlocks: [],
    })
    const toolCall = toolCallFromApiMessage(message, id)
    assistant.toolCalls = [...(assistant.toolCalls || []), toolCall]
    appendToolBlock(assistant, toolCall)
    return
  }

  if (message.content_type === 'thinking') {
    const assistant = ensureAssistant(state, id, timestamp)
    assistant.thinkingContent = (assistant.thinkingContent || '') + (message.content || '')
    return
  }

  if (content.startsWith('[{') && content.includes('tool_use')) {
    const toolCalls = protocolToolCallsFromContent(content, id)
    if (toolCalls) {
      appendProtocolToolCalls(state, id, timestamp, toolCalls)
      return
    }
    appendAssistantText(state, id, timestamp, content)
    return
  }

  appendAssistantText(state, id, timestamp, message.content || '')
}

function mapToolApiMessage(state: ApiMappingState, message: ApiMessage) {
  if (state.currentAssistant) {
    completeToolCallFromMessage(state.currentAssistant, message)
  }
}

export function mapApiMessages(messages: ApiMessage[]): ChatMessage[] {
  const state: ApiMappingState = { result: [], currentAssistant: null }

  for (const message of messages) {
    const id = message.id || `msg-${message.message_index ?? state.result.length}`
    const timestamp = new Date(message.timestamp)

    if (message.content_blocks && message.content_blocks.length > 0) {
      mapContentBlockMessage(state, message, id, timestamp)
      continue
    }

    const content = (message.content || '').trim()

    if (message.role === 'user') {
      mapUserApiMessage(state, message, id, timestamp, content)
    } else if (message.role === 'assistant') {
      mapAssistantApiMessage(state, message, id, timestamp, content)
    } else if (message.role === 'tool') {
      mapToolApiMessage(state, message)
    }
  }

  flushAssistant(state)
  return state.result
}
