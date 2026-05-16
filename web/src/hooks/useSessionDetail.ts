import { useState, useEffect, useCallback, useRef } from 'react'
import type { GobbySession } from '../types/sessions'
import { useWebSocketEvent } from './useWebSocketEvent'
import type { ContentBlock, TokenUsage, ToolCall } from '../types/chat'

export interface SessionMessage {
  id: string
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

export interface TranscriptStatus {
  session_id: string
  live_exists: boolean
  archive_exists: boolean
  availability: 'live' | 'archive_only' | 'missing'
  content_state: 'messages' | 'empty' | 'unparseable' | 'missing'
  session_source?: string | null
  detected_source?: string | null
  source_mismatch: boolean
  raw_record_count: number
  parsed_message_count: number
}

interface MessageLoadResult {
  mapped: SessionMessage[]
  totalCount: number
  ok: boolean
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

const CHAT_MESSAGES_POLL_MS = 2000
const SESSION_METADATA_UNAVAILABLE_MESSAGE =
  'Session metadata is unavailable. It may have expired or been deleted.'

async function sessionMetadataFetchError(response: Response): Promise<Error> {
  const body = await response.text().catch(() => '')
  const detail = body.trim() ? `: ${body.trim()}` : ''
  return new Error(`Session metadata fetch failed (${response.status} ${response.statusText})${detail}`)
}

async function fetchSessionMetadata(sessionId: string): Promise<GobbySession | null> {
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

function mapRenderedRecordToSessionMessage(message: Record<string, unknown>): SessionMessage {
  return {
    id: String(message.id ?? message.message_index ?? `hist-${Math.random()}`),
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

function mapWebChatRecordToSessionMessage(message: Record<string, unknown>): SessionMessage {
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
    id: String(message.id ?? `chat-${Math.random()}`),
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

async function fetchRenderedSessionMessages(sessionId: string): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const messagesRes = await fetch(
    `${baseUrl}/api/sessions/${sessionId}/messages?limit=10000&offset=0`,
  )
  if (!messagesRes.ok) {
    console.warn(`Messages fetch returned ${messagesRes.status}`)
    return { mapped: [], totalCount: 0, ok: false }
  }
  const messageData = await messagesRes.json()
  const rawMessages = Array.isArray(messageData?.messages) ? messageData.messages : []
  return {
    mapped: rawMessages.map((m: Record<string, unknown>) =>
      mapRenderedRecordToSessionMessage(m),
    ),
    totalCount: messageData.total_count || rawMessages.length,
    ok: true,
  }
}

async function fetchChatSessionMessages(sessionId: string): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const chatRes = await fetch(`${baseUrl}/api/chat/${sessionId}/messages`)
  if (!chatRes.ok) {
    console.warn(`Web chat messages fetch returned ${chatRes.status}`)
    return { mapped: [], totalCount: 0, ok: false }
  }
  const chatData = await chatRes.json()
  const rawMessages = Array.isArray(chatData?.messages) ? chatData.messages : []
  return {
    mapped: rawMessages.map((m: Record<string, unknown>) =>
      mapWebChatRecordToSessionMessage(m),
    ),
    totalCount: rawMessages.length,
    ok: true,
  }
}

async function fetchTranscriptStatus(sessionId: string): Promise<TranscriptStatus | null> {
  const baseUrl = getBaseUrl()
  const statusRes = await fetch(`${baseUrl}/api/sessions/${sessionId}/transcript/status`)
  if (!statusRes.ok) {
    console.warn(`Transcript status fetch returned ${statusRes.status}`)
    return null
  }
  return (await statusRes.json()) as TranscriptStatus
}

export function useSessionDetail(sessionId: string | null) {
  const [session, setSession] = useState<GobbySession | null>(null)
  const [messages, setMessages] = useState<SessionMessage[]>([])
  const [transcriptStatus, setTranscriptStatus] = useState<TranscriptStatus | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [totalMessages, setTotalMessages] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const messageSourceRef = useRef<'session' | 'chat' | null>(null)
  const sessionIdRef = useRef(sessionId)
  const detailPollCleanupRef = useRef<(() => void) | null>(null)
  const detailLoadVersionRef = useRef(0)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  const clearDetailPolling = useCallback(() => {
    detailPollCleanupRef.current?.()
    detailPollCleanupRef.current = null
  }, [])

  const applyClearedDetail = useCallback((error: string | null) => {
    setSession(null)
    setMessages([])
    setTranscriptStatus(null)
    setSessionError(error)
    setTotalMessages(0)
    setIsLoading(false)
    messageSourceRef.current = null
  }, [])

  const resetSessionDetail = useCallback((error: string | null) => {
    detailLoadVersionRef.current += 1
    clearDetailPolling()
    applyClearedDetail(error)
  }, [applyClearedDetail, clearDetailPolling])

  const clearSessionError = useCallback(() => {
    setSessionError(null)
  }, [])

  const loadSessionDetail = useCallback(async (
    activeSessionId: string,
    {
      showLoading = true,
      clearOnError = true,
      errorMessage = 'Failed to load session detail',
    }: {
      showLoading?: boolean
      clearOnError?: boolean
      errorMessage?: string
    } = {},
  ) => {
    const loadVersion = detailLoadVersionRef.current + 1
    detailLoadVersionRef.current = loadVersion
    const isCurrent = () =>
      sessionIdRef.current === activeSessionId && detailLoadVersionRef.current === loadVersion

    if (showLoading) {
      setIsLoading(true)
    }
    setSessionError(null)

    try {
      const sessionData = await fetchSessionMetadata(activeSessionId)
      if (!isCurrent()) return

      clearDetailPolling()

      if (!sessionData) {
        applyClearedDetail(SESSION_METADATA_UNAVAILABLE_MESSAGE)
        return
      }

      setSession(sessionData)
      setSessionError(null)
      setTranscriptStatus(null)

      const renderedResult = await fetchRenderedSessionMessages(activeSessionId)
      if (!isCurrent()) return

      const shouldUseChatMessages =
        sessionData.session_type === 'web_chat' &&
        !sessionData.transcript_path &&
        renderedResult.mapped.length === 0

      if (shouldUseChatMessages) {
        const chatResult = await fetchChatSessionMessages(activeSessionId)
        if (!isCurrent()) return

        messageSourceRef.current = 'chat'
        setTranscriptStatus(null)
        setMessages(chatResult.mapped)
        setTotalMessages(chatResult.totalCount)

        let cancelled = false
        let pollTimeoutId: number | null = null
        const pollChatMessages = async () => {
          if (cancelled || !isCurrent() || messageSourceRef.current !== 'chat') return
          try {
            const nextChatResult = await fetchChatSessionMessages(activeSessionId)
            if (
              cancelled ||
              !isCurrent() ||
              messageSourceRef.current !== 'chat' ||
              !nextChatResult.ok
            ) {
              return
            }
            setMessages(nextChatResult.mapped)
            setTotalMessages(nextChatResult.totalCount)
          } catch (error) {
            console.warn('Failed to poll web chat messages:', error)
          } finally {
            if (!cancelled && isCurrent() && messageSourceRef.current === 'chat') {
              pollTimeoutId = window.setTimeout(() => {
                void pollChatMessages()
              }, CHAT_MESSAGES_POLL_MS)
            }
          }
        }
        pollTimeoutId = window.setTimeout(() => {
          void pollChatMessages()
        }, CHAT_MESSAGES_POLL_MS)
        detailPollCleanupRef.current = () => {
          cancelled = true
          if (pollTimeoutId !== null) {
            window.clearTimeout(pollTimeoutId)
          }
        }
        return
      }

      messageSourceRef.current = 'session'
      setMessages(renderedResult.mapped)
      setTotalMessages(renderedResult.totalCount)
      if (renderedResult.mapped.length === 0) {
        const nextTranscriptStatus = await fetchTranscriptStatus(activeSessionId)
        if (isCurrent()) {
          setTranscriptStatus(nextTranscriptStatus)
        }
      }
    } catch (e) {
      console.error('Failed to fetch session detail:', e)
      if (!isCurrent()) return
      if (clearOnError) {
        clearDetailPolling()
        applyClearedDetail(errorMessage)
      } else {
        setSessionError(errorMessage)
      }
    } finally {
      if (showLoading && isCurrent()) {
        setIsLoading(false)
      }
    }
  }, [applyClearedDetail, clearDetailPolling])

  // Fetch session detail and all messages
  useEffect(() => {
    if (!sessionId) {
      resetSessionDetail(null)
      return
    }

    void loadSessionDetail(sessionId)

    return () => {
      detailLoadVersionRef.current += 1
      clearDetailPolling()
    }
  }, [clearDetailPolling, loadSessionDetail, resetSessionDetail, sessionId])

  // Subscribe to real-time session_message events via WebSocket
  // Broadcasts are now RenderedMessage-shaped with content_blocks.
  // Uses upsert semantics: replace existing message with same ID, append if new.
  useWebSocketEvent('session_message', useCallback((data: Record<string, unknown>) => {
    const msgSessionId = data.session_id as string | undefined
    if (!msgSessionId || msgSessionId !== sessionIdRef.current) return

    const msg = data.message as Record<string, unknown> | undefined
    if (!msg) return

    messageSourceRef.current = 'session'
    const newMessage = mapRenderedRecordToSessionMessage(msg)

    setMessages((prev) => {
      const existingIdx = prev.findIndex((m) => m.id === newMessage.id)
      if (existingIdx >= 0) {
        // Upsert: replace existing message (in-progress turn update)
        const updated = [...prev]
        updated[existingIdx] = newMessage
        return updated
      }
      // Only increment total for genuinely new messages, not upserts
      setTotalMessages((p) => p + 1)
      return [...prev, newMessage]
    })
  }, []))

  useWebSocketEvent('session_usage_updated', useCallback((data: Record<string, unknown>) => {
    const updatedSessionId = typeof data.session_id === 'string' ? data.session_id : null
    if (!updatedSessionId || updatedSessionId !== sessionIdRef.current) return

    setSession((prev) =>
      prev
        ? {
            ...prev,
            usage_input_tokens:
              typeof data.usage_input_tokens === 'number'
                ? data.usage_input_tokens
                : prev.usage_input_tokens,
            usage_output_tokens:
              typeof data.usage_output_tokens === 'number'
                ? data.usage_output_tokens
                : prev.usage_output_tokens,
            usage_cache_creation_tokens:
              typeof data.usage_cache_creation_tokens === 'number'
                ? data.usage_cache_creation_tokens
                : prev.usage_cache_creation_tokens,
            usage_cache_read_tokens:
              typeof data.usage_cache_read_tokens === 'number'
                ? data.usage_cache_read_tokens
                : prev.usage_cache_read_tokens,
            context_window:
              typeof data.context_window === 'number'
                ? data.context_window
                : prev.context_window,
            model: typeof data.model === 'string' ? data.model : prev.model,
          }
        : prev,
    )
  }, []))

  useWebSocketEvent('session_event', useCallback((data: Record<string, unknown>) => {
    const event = typeof data.event === 'string' ? data.event : null
    const updatedSessionId = typeof data.session_id === 'string' ? data.session_id : null
    if (!updatedSessionId || updatedSessionId !== sessionIdRef.current) return

    if (event === 'session_deleted') {
      resetSessionDetail(SESSION_METADATA_UNAVAILABLE_MESSAGE)
      return
    }

    if (event === 'session_updated' || event === 'session_expired') {
      void loadSessionDetail(updatedSessionId, {
        showLoading: false,
        clearOnError: false,
        errorMessage: 'Failed to refresh session metadata',
      })
    }
  }, [loadSessionDetail, resetSessionDetail]))

  const hasMore = false

  const [isGeneratingSummary, setIsGeneratingSummary] = useState(false)

  const generateSummary = useCallback(async () => {
    if (!sessionId || isGeneratingSummary) return

    const baseUrl = getBaseUrl()
    setIsGeneratingSummary(true)
    try {
      const res = await fetch(`${baseUrl}/api/sessions/${sessionId}/generate-summary`, {
        method: 'POST',
      })
      if (res.ok) {
        const data = await res.json()
        if (data.summary_markdown) {
          setSession((prev) =>
            prev ? { ...prev, summary_markdown: data.summary_markdown } : prev
          )
        }
      } else {
        const err = await res.json().catch(() => null)
        console.error('Failed to generate summary:', err?.detail || res.statusText)
      }
    } catch (e) {
      console.error('Failed to generate summary:', e)
    } finally {
      setIsGeneratingSummary(false)
    }
  }, [sessionId, isGeneratingSummary])

  // loadMore kept as no-op for interface compatibility
  const loadMore = useCallback(() => {}, [])

  return {
    session,
    sessionError,
    clearSessionError,
    messages,
    transcriptStatus,
    isLoading,
    totalMessages,
    hasMore,
    loadMore,
    generateSummary,
    isGeneratingSummary,
  }
}
