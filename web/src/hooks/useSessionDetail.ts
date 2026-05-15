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

export function useSessionDetail(sessionId: string | null) {
  const [session, setSession] = useState<GobbySession | null>(null)
  const [messages, setMessages] = useState<SessionMessage[]>([])
  const [transcriptStatus, setTranscriptStatus] = useState<TranscriptStatus | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [totalMessages, setTotalMessages] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const messageSourceRef = useRef<'session' | 'chat' | null>(null)
  const sessionIdRef = useRef(sessionId)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  // Fetch session detail and all messages
  useEffect(() => {
    if (!sessionId) {
      setSession(null)
      setMessages([])
      setTranscriptStatus(null)
      setSessionError(null)
      setTotalMessages(0)
      messageSourceRef.current = null
      return
    }

    const activeSessionId = sessionId
    let cancelled = false
    setIsLoading(true)
    setSessionError(null)

    async function fetchDetail() {
      const baseUrl = getBaseUrl()
      try {
        const sessionData = await fetchSessionMetadata(activeSessionId)

        if (cancelled) return

        if (sessionData) {
          setSession(sessionData)
          setSessionError(null)
          setTranscriptStatus(null)

          const loadRenderedMessages = async (): Promise<{
            mapped: SessionMessage[]
            totalCount: number
            ok: boolean
          }> => {
            const messagesRes = await fetch(
              `${baseUrl}/api/sessions/${activeSessionId}/messages?limit=10000&offset=0`,
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

          const loadChatMessages = async (): Promise<{
            mapped: SessionMessage[]
            totalCount: number
            ok: boolean
          }> => {
            const chatRes = await fetch(`${baseUrl}/api/chat/${activeSessionId}/messages`)
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

          const loadTranscriptStatus = async (): Promise<TranscriptStatus | null> => {
            const statusRes = await fetch(
              `${baseUrl}/api/sessions/${activeSessionId}/transcript/status`,
            )
            if (!statusRes.ok) {
              console.warn(`Transcript status fetch returned ${statusRes.status}`)
              return null
            }
            return (await statusRes.json()) as TranscriptStatus
          }

          const renderedResult = await loadRenderedMessages()
          if (cancelled) return

          const shouldUseChatMessages =
            sessionData?.session_type === 'web_chat' &&
            !sessionData?.transcript_path &&
            renderedResult.mapped.length === 0

          if (shouldUseChatMessages) {
            const chatResult = await loadChatMessages()
            if (cancelled) return

            messageSourceRef.current = 'chat'
            setTranscriptStatus(null)
            setMessages(chatResult.mapped)
            setTotalMessages(chatResult.totalCount)

            const pollId = window.setInterval(async () => {
              if (cancelled || messageSourceRef.current !== 'chat') return
              const nextChatResult = await loadChatMessages()
              if (cancelled || messageSourceRef.current !== 'chat' || !nextChatResult.ok) return
              setMessages(nextChatResult.mapped)
              setTotalMessages(nextChatResult.totalCount)
            }, CHAT_MESSAGES_POLL_MS)

            return () => window.clearInterval(pollId)
          }

          messageSourceRef.current = 'session'
          setMessages(renderedResult.mapped)
          setTotalMessages(renderedResult.totalCount)
          if (renderedResult.mapped.length === 0) {
            const nextTranscriptStatus = await loadTranscriptStatus()
            if (!cancelled) {
              setTranscriptStatus(nextTranscriptStatus)
            }
          }
        } else {
          setSession(null)
          setMessages([])
          setTranscriptStatus(null)
          setSessionError(SESSION_METADATA_UNAVAILABLE_MESSAGE)
          setTotalMessages(0)
          messageSourceRef.current = null
        }
      } catch (e) {
        console.error('Failed to fetch session detail:', e)
        if (!cancelled) {
          setSession(null)
          setMessages([])
          setTranscriptStatus(null)
          setSessionError('Failed to load session detail')
          setTotalMessages(0)
          messageSourceRef.current = null
        }
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }

    let cleanup: (() => void) | undefined

    fetchDetail().then((teardown) => {
      if (cancelled) {
        teardown?.()
        return
      }
      cleanup = teardown
    })

    return () => {
      cancelled = true
      cleanup?.()
    }
  }, [sessionId])

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

  const refreshSelectedSessionMetadata = useCallback(async (updatedSessionId: string) => {
    try {
      const refreshed = await fetchSessionMetadata(updatedSessionId)
      if (sessionIdRef.current !== updatedSessionId) return
      if (!refreshed) {
        setSession(null)
        setMessages([])
        setTranscriptStatus(null)
        setSessionError(SESSION_METADATA_UNAVAILABLE_MESSAGE)
        setTotalMessages(0)
        messageSourceRef.current = null
        return
      }
      setSessionError(null)
      setSession(refreshed)
    } catch (e) {
      console.error('Failed to refresh session metadata:', e)
      if (sessionIdRef.current === updatedSessionId) {
        setSessionError('Failed to refresh session metadata')
      }
    }
  }, [])

  useWebSocketEvent('session_event', useCallback((data: Record<string, unknown>) => {
    const event = typeof data.event === 'string' ? data.event : null
    const updatedSessionId = typeof data.session_id === 'string' ? data.session_id : null
    if (!updatedSessionId || updatedSessionId !== sessionIdRef.current) return

    if (event === 'session_deleted') {
      setSession(null)
      setMessages([])
      setTranscriptStatus(null)
      setSessionError(SESSION_METADATA_UNAVAILABLE_MESSAGE)
      setTotalMessages(0)
      messageSourceRef.current = null
      return
    }

    if (event === 'session_updated' || event === 'session_expired') {
      void refreshSelectedSessionMetadata(updatedSessionId)
    }
  }, [refreshSelectedSessionMetadata]))

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
