import type { ContentBlock, ToolCall, TokenUsage } from '../../types/chat'
import type { GobbySession } from '../../types/sessions'
import { TRANSCRIPT_PAGE_SIZE } from '../sessionTranscriptWindow'
import type { MessageLoadResult, SessionMessage, TranscriptStatus } from './types'

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

async function sessionMetadataFetchError(response: Response): Promise<Error> {
  const body = await response.text().catch(() => '')
  const detail = body.trim() ? `: ${body.trim()}` : ''
  return new Error(`Session metadata fetch failed (${response.status} ${response.statusText})${detail}`)
}

function stableFallbackMessageId(
  prefix: 'chat' | 'hist',
  message: Record<string, unknown>,
  fallbackIndex: number,
): string {
  const raw = [
    fallbackIndex,
    message.message_index,
    message.created_at,
    message.timestamp,
    message.role,
    message.content_type,
    message.tool_name,
    message.content,
  ].map((value) => String(value ?? '')).join('\u001f')
  let hash = 0
  for (let index = 0; index < raw.length; index += 1) {
    hash = (hash * 31 + raw.charCodeAt(index)) >>> 0
  }
  return `${prefix}-${fallbackIndex}-${hash.toString(36)}`
}

export async function fetchSessionMetadata(sessionId: string): Promise<GobbySession | null> {
  const baseUrl = getBaseUrl()
  const sessionRes = await fetch(`${baseUrl}/api/sessions/${sessionId}`)
  if (!sessionRes.ok) {
    console.warn(`Session fetch returned ${sessionRes.status}`)
    if (sessionRes.status === 404) {
      return null
    }
    throw await sessionMetadataFetchError(sessionRes)
  }
  const data = await sessionRes.json()
  return (data.session || null) as GobbySession | null
}

export function mapRenderedRecordToSessionMessage(
  message: Record<string, unknown>,
  fallbackIndex = 0,
): SessionMessage {
  return {
    id: String(
      message.id ?? message.message_index ?? stableFallbackMessageId('hist', message, fallbackIndex),
    ),
    role: (message.role as string) ?? 'assistant',
    content: (message.content as string) ?? '',
    timestamp: (message.timestamp as string) ?? '',
    content_blocks: message.content_blocks as ContentBlock[] | undefined,
    model: message.model as string | null | undefined,
    usage: message.usage as TokenUsage | null | undefined,
    content_type: message.content_type as string | undefined,
    tool_name: message.tool_name as string | undefined,
    message_index: message.message_index as number | undefined,
  }
}

function mapWebChatRecordToSessionMessage(
  message: Record<string, unknown>,
  fallbackIndex = 0,
): SessionMessage {
  const content = (message.content as string) ?? ''
  const contentBlocks: ContentBlock[] = []
  if (content) {
    contentBlocks.push({ type: 'text', content })
  }

  const toolCalls = Array.isArray(message.tool_calls)
    ? (message.tool_calls as ToolCall[])
    : []
  if (toolCalls.length > 0) {
    contentBlocks.push({ type: 'tool_chain', tool_calls: toolCalls })
  }

  return {
    id: String(message.id ?? stableFallbackMessageId('chat', message, fallbackIndex)),
    role: (message.role as string) ?? 'assistant',
    content,
    timestamp:
      (message.created_at as string) ??
      (message.timestamp as string) ??
      new Date().toISOString(),
    content_blocks: contentBlocks.length > 0 ? contentBlocks : undefined,
    model: (message.model as string | null | undefined) ?? null,
    usage: null,
  }
}

export async function fetchRenderedSessionMessages(
  sessionId: string,
  offset: number,
  order: 'head' | 'tail',
): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const messagesRes = await fetch(
    `${baseUrl}/api/sessions/${sessionId}/messages?limit=${TRANSCRIPT_PAGE_SIZE}&offset=${offset}&order=${order}`,
  )
  if (!messagesRes.ok) {
    console.warn(`Messages fetch returned ${messagesRes.status}`)
    return {
      mapped: [],
      totalCount: 0,
      renderedTotal: 0,
      returnedCount: 0,
      degradedReason: null,
      ok: false,
    }
  }
  const messageData = await messagesRes.json()
  const rawMessages = Array.isArray(messageData?.messages) ? messageData.messages : []
  const mapped = rawMessages.map((m: Record<string, unknown>, index: number) =>
    mapRenderedRecordToSessionMessage(m, offset + index),
  )
  const returnedCount =
    typeof messageData.returned_count === 'number' ? messageData.returned_count : mapped.length
  return {
    mapped,
    totalCount: typeof messageData.total_count === 'number' ? messageData.total_count : mapped.length,
    renderedTotal:
      typeof messageData.rendered_count === 'number' ? messageData.rendered_count : mapped.length,
    returnedCount,
    degradedReason: messageData.degraded
      ? ((messageData.degraded_reason as string) ?? 'max_span_exceeded')
      : null,
    ok: true,
  }
}

export async function fetchChatSessionMessages(sessionId: string): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const chatRes = await fetch(`${baseUrl}/api/chat/${sessionId}/messages`)
  if (!chatRes.ok) {
    console.warn(`Web chat messages fetch returned ${chatRes.status}`)
    return {
      mapped: [],
      totalCount: 0,
      renderedTotal: 0,
      returnedCount: 0,
      degradedReason: null,
      ok: false,
    }
  }
  const chatData = await chatRes.json()
  const rawMessages = Array.isArray(chatData?.messages) ? chatData.messages : []
  const mapped = rawMessages.map((m: Record<string, unknown>, index: number) =>
    mapWebChatRecordToSessionMessage(m, index),
  )
  return {
    mapped,
    totalCount: mapped.length,
    renderedTotal: mapped.length,
    returnedCount: mapped.length,
    degradedReason: null,
    ok: true,
  }
}

export async function fetchTranscriptStatus(sessionId: string): Promise<TranscriptStatus | null> {
  const baseUrl = getBaseUrl()
  const statusRes = await fetch(`${baseUrl}/api/sessions/${sessionId}/transcript/status`)
  if (!statusRes.ok) {
    console.warn(`Transcript status fetch returned ${statusRes.status}`)
    return null
  }
  return (await statusRes.json()) as TranscriptStatus
}

export function toTranscriptWindowPage(result: MessageLoadResult) {
  return {
    messages: result.mapped,
    renderedTotal: result.renderedTotal,
    returnedCount: result.returnedCount,
  }
}

export async function generateSessionSummary(sessionId: string): Promise<string | null> {
  const baseUrl = getBaseUrl()
  const res = await fetch(`${baseUrl}/api/sessions/${sessionId}/generate-summary`, {
    method: 'POST',
  })
  if (!res.ok) {
    const err = await res.json().catch(() => null)
    console.error('Failed to generate summary:', err?.detail || res.statusText)
    return null
  }

  const data = await res.json()
  return typeof data.summary_markdown === 'string' ? data.summary_markdown : null
}
