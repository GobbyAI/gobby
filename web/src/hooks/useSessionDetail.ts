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
  renderedTotal: number
  returnedCount: number
  degradedReason: string | null
  ok: boolean
}

// Tail-first paging: load the newest page, then prepend older pages on scroll-up.
const PAGE = 50
// Virtuoso prepend anchor — large so it can decrement as older pages load.
const START_INDEX = 1_000_000
const TAIL_REFRESH_DEBOUNCE_MS = 500

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

async function fetchRenderedSessionMessages(
  sessionId: string,
  offset: number,
  order: 'head' | 'tail',
): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const messagesRes = await fetch(
    `${baseUrl}/api/sessions/${sessionId}/messages?limit=${PAGE}&offset=${offset}&order=${order}`,
  )
  if (!messagesRes.ok) {
    console.warn(`Messages fetch returned ${messagesRes.status}`)
    return { mapped: [], totalCount: 0, renderedTotal: 0, returnedCount: 0, degradedReason: null, ok: false }
  }
  const messageData = await messagesRes.json()
  const rawMessages = Array.isArray(messageData?.messages) ? messageData.messages : []
  const mapped = rawMessages.map((m: Record<string, unknown>) =>
    mapRenderedRecordToSessionMessage(m),
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

async function fetchChatSessionMessages(sessionId: string): Promise<MessageLoadResult> {
  const baseUrl = getBaseUrl()
  const chatRes = await fetch(`${baseUrl}/api/chat/${sessionId}/messages`)
  if (!chatRes.ok) {
    console.warn(`Web chat messages fetch returned ${chatRes.status}`)
    return { mapped: [], totalCount: 0, renderedTotal: 0, returnedCount: 0, degradedReason: null, ok: false }
  }
  const chatData = await chatRes.json()
  const rawMessages = Array.isArray(chatData?.messages) ? chatData.messages : []
  const mapped = rawMessages.map((m: Record<string, unknown>) =>
    mapWebChatRecordToSessionMessage(m),
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
  // Rendered-group pagination state (group counts, not parsed-message counts).
  const [loadedCount, setLoadedCount] = useState(0)
  const [renderedTotal, setRenderedTotal] = useState(0)
  const [isLoadingOlder, setIsLoadingOlder] = useState(false)
  const [firstItemIndex, setFirstItemIndex] = useState(START_INDEX)
  const [transcriptDegradedReason, setTranscriptDegradedReason] = useState<string | null>(null)

  const messageSourceRef = useRef<'session' | 'chat' | null>(null)
  const messagesRef = useRef<SessionMessage[]>([])
  const sessionIdRef = useRef(sessionId)
  const sessionRef = useRef<GobbySession | null>(null)
  const detailPollCleanupRef = useRef<(() => void) | null>(null)
  const tailRefreshTimeoutRef = useRef<number | null>(null)
  const detailLoadVersionRef = useRef(0)
  // Mirrors of paging state read synchronously by loadMore / live append.
  const loadedCountRef = useRef(0)
  const renderedTotalRef = useRef(0)
  const loadingOlderRef = useRef(false)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  useEffect(() => {
    sessionRef.current = session
  }, [session])

  useEffect(() => {
    messagesRef.current = messages
  }, [messages])

  useEffect(() => {
    loadedCountRef.current = loadedCount
  }, [loadedCount])

  useEffect(() => {
    renderedTotalRef.current = renderedTotal
  }, [renderedTotal])

  const clearDetailPolling = useCallback(() => {
    detailPollCleanupRef.current?.()
    detailPollCleanupRef.current = null
  }, [])

  const clearTailRefresh = useCallback(() => {
    if (tailRefreshTimeoutRef.current !== null) {
      window.clearTimeout(tailRefreshTimeoutRef.current)
      tailRefreshTimeoutRef.current = null
    }
  }, [])

  const resetPaging = useCallback(() => {
    setLoadedCount(0)
    setRenderedTotal(0)
    loadedCountRef.current = 0
    renderedTotalRef.current = 0
    loadingOlderRef.current = false
    setIsLoadingOlder(false)
    setFirstItemIndex(START_INDEX)
    setTranscriptDegradedReason(null)
  }, [])

  const applyClearedDetail = useCallback((error: string | null) => {
    setSession(null)
    messagesRef.current = []
    setMessages([])
    setTranscriptStatus(null)
    setSessionError(error)
    setTotalMessages(0)
    setIsLoading(false)
    messageSourceRef.current = null
    resetPaging()
  }, [resetPaging])

  const resetSessionDetail = useCallback((error: string | null) => {
    detailLoadVersionRef.current += 1
    clearDetailPolling()
    clearTailRefresh()
    applyClearedDetail(error)
  }, [applyClearedDetail, clearDetailPolling, clearTailRefresh])

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
    clearTailRefresh()
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
      resetPaging()

      // Tail-first: open at the newest page, page older on scroll-up.
      const renderedResult = await fetchRenderedSessionMessages(activeSessionId, 0, 'tail')
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
        messagesRef.current = chatResult.mapped
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
            messagesRef.current = nextChatResult.mapped
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
      messagesRef.current = renderedResult.mapped
      setMessages(renderedResult.mapped)
      setTotalMessages(renderedResult.totalCount)
      setLoadedCount(renderedResult.returnedCount)
      setRenderedTotal(renderedResult.renderedTotal)
      loadedCountRef.current = renderedResult.returnedCount
      renderedTotalRef.current = renderedResult.renderedTotal
      setTranscriptDegradedReason(renderedResult.degradedReason)
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
  }, [applyClearedDetail, clearDetailPolling, clearTailRefresh, resetPaging])

  const applyRefreshedTailMessages = useCallback((result: MessageLoadResult) => {
    const currentMessages = messagesRef.current
    const currentIds = new Set(currentMessages.map((message) => message.id))
    const refreshedById = new Map(result.mapped.map((message) => [message.id, message]))
    let overlapCount = 0

    const replacedMessages = currentMessages.map((message) => {
      const refreshed = refreshedById.get(message.id)
      if (!refreshed) return message
      overlapCount += 1
      return refreshed
    })
    const appendedMessages = result.mapped.filter((message) => !currentIds.has(message.id))
    const nextMessages =
      appendedMessages.length > 0
        ? [...replacedMessages, ...appendedMessages]
        : replacedMessages
    const appendedCount = appendedMessages.length

    messagesRef.current = nextMessages
    setMessages(nextMessages)
    setTotalMessages((prev) => Math.max(result.totalCount, prev + appendedCount))

    const preservedLoadedCount = Math.max(loadedCountRef.current - overlapCount, 0)
    const nextLoadedCount = preservedLoadedCount + result.returnedCount
    const nextRenderedTotal = Math.max(
      result.renderedTotal,
      renderedTotalRef.current + appendedCount,
      nextLoadedCount,
    )
    loadedCountRef.current = nextLoadedCount
    renderedTotalRef.current = nextRenderedTotal
    setLoadedCount(nextLoadedCount)
    setRenderedTotal(nextRenderedTotal)
    setTranscriptDegradedReason(result.degradedReason)
    if (nextMessages.length > 0) {
      setTranscriptStatus(null)
    }
  }, [])

  const scheduleTailRefresh = useCallback((activeSessionId: string) => {
    if (messageSourceRef.current !== 'session') return

    clearTailRefresh()
    tailRefreshTimeoutRef.current = window.setTimeout(() => {
      tailRefreshTimeoutRef.current = null
      if (sessionIdRef.current !== activeSessionId || messageSourceRef.current !== 'session') return

      const refreshVersion = detailLoadVersionRef.current
      void (async () => {
        try {
          const result = await fetchRenderedSessionMessages(activeSessionId, 0, 'tail')
          if (
            sessionIdRef.current !== activeSessionId ||
            detailLoadVersionRef.current !== refreshVersion ||
            messageSourceRef.current !== 'session' ||
            !result.ok
          ) {
            return
          }
          applyRefreshedTailMessages(result)
        } catch (error) {
          if (sessionIdRef.current === activeSessionId) {
            console.warn('Failed to refresh session tail messages:', error)
          }
        }
      })()
    }, TAIL_REFRESH_DEBOUNCE_MS)
  }, [applyRefreshedTailMessages, clearTailRefresh])

  // Refresh only session metadata; reload the transcript only when its identity
  // (path/source) changed. Otherwise debounce a tail-page refresh so missed
  // session_message events don't leave the selected transcript stale.
  const refreshSessionMetadata = useCallback(async (activeSessionId: string) => {
    try {
      const meta = await fetchSessionMetadata(activeSessionId)
      if (sessionIdRef.current !== activeSessionId) return
      if (!meta) {
        resetSessionDetail(SESSION_METADATA_UNAVAILABLE_MESSAGE)
        return
      }
      const current = sessionRef.current
      const identityChanged =
        current != null &&
        (current.transcript_path !== meta.transcript_path || current.source !== meta.source)
      if (identityChanged) {
        void loadSessionDetail(activeSessionId, {
          showLoading: false,
          clearOnError: false,
          errorMessage: 'Failed to refresh session metadata',
        })
      } else {
        setSession(meta)
        scheduleTailRefresh(activeSessionId)
      }
    } catch (error) {
      if (sessionIdRef.current !== activeSessionId) return
      console.error('Failed to refresh session metadata:', error)
      // Surface a non-clearing error; keep the loaded detail and scroll intact.
      setSessionError('Failed to refresh session metadata')
    }
  }, [loadSessionDetail, resetSessionDetail, scheduleTailRefresh])

  // Load an older page (scroll-up). Prepends the oldest-first window above the
  // current messages and decrements the Virtuoso anchor by the actual count.
  const loadMore = useCallback(async () => {
    const activeSessionId = sessionIdRef.current
    if (!activeSessionId) return
    if (messageSourceRef.current !== 'session') return
    if (loadingOlderRef.current) return
    if (loadedCountRef.current >= renderedTotalRef.current) return

    const loadVersion = detailLoadVersionRef.current
    const requestedOffset = loadedCountRef.current
    loadingOlderRef.current = true
    setIsLoadingOlder(true)
    try {
      const result = await fetchRenderedSessionMessages(
        activeSessionId,
        requestedOffset,
        'tail',
      )
      if (sessionIdRef.current !== activeSessionId || detailLoadVersionRef.current !== loadVersion) {
        return
      }
      if (!result.ok || result.mapped.length === 0) return

      setMessages((prev) => {
        const next = [...result.mapped, ...prev]
        messagesRef.current = next
        return next
      })
      setFirstItemIndex((prev) => prev - result.returnedCount)
      const nextLoaded = Math.max(
        loadedCountRef.current,
        requestedOffset + result.returnedCount,
      )
      const nextTotal = Math.max(renderedTotalRef.current, result.renderedTotal)
      loadedCountRef.current = nextLoaded
      renderedTotalRef.current = nextTotal
      setLoadedCount(nextLoaded)
      setRenderedTotal(nextTotal)
      if (result.degradedReason) {
        setTranscriptDegradedReason(result.degradedReason)
      }
    } finally {
      loadingOlderRef.current = false
      if (sessionIdRef.current === activeSessionId) {
        setIsLoadingOlder(false)
      }
    }
  }, [])

  // Fetch session detail and the newest page of messages
  useEffect(() => {
    if (!sessionId) {
      resetSessionDetail(null)
      return
    }

    void loadSessionDetail(sessionId)

    return () => {
      detailLoadVersionRef.current += 1
      clearDetailPolling()
      clearTailRefresh()
    }
  }, [clearDetailPolling, clearTailRefresh, loadSessionDetail, resetSessionDetail, sessionId])

  // Subscribe to real-time session_message events via WebSocket.
  // Broadcasts are RenderedMessage-shaped with content_blocks. Upsert by id;
  // a genuinely new group is appended at the tail and advances the loaded/total
  // group counts so the next tail page request doesn't overlap.
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
        messagesRef.current = updated
        return updated
      }
      // Genuinely new group: bump parsed total and the rendered-group counts.
      setTotalMessages((p) => p + 1)
      setLoadedCount((p) => {
        const next = p + 1
        loadedCountRef.current = next
        return next
      })
      setRenderedTotal((p) => {
        const next = p + 1
        renderedTotalRef.current = next
        return next
      })
      const next = [...prev, newMessage]
      messagesRef.current = next
      return next
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
            context_used_tokens:
              typeof data.context_used_tokens === 'number'
                ? data.context_used_tokens
                : data.context_used_tokens === null
                  ? null
                  : prev.context_used_tokens,
            context_usage_ratio:
              typeof data.context_usage_ratio === 'number'
                ? data.context_usage_ratio
                : data.context_usage_ratio === null
                  ? null
                  : prev.context_usage_ratio,
            context_usage_source:
              typeof data.context_usage_source === 'string'
                ? data.context_usage_source
                : data.context_usage_source === null
                  ? null
                  : prev.context_usage_source,
            context_usage_confidence:
              typeof data.context_usage_confidence === 'string'
                ? data.context_usage_confidence
                : data.context_usage_confidence === null
                  ? null
                  : prev.context_usage_confidence,
            last_prompt_input_tokens:
              typeof data.last_prompt_input_tokens === 'number'
                ? data.last_prompt_input_tokens
                : data.last_prompt_input_tokens === null
                  ? null
                  : prev.last_prompt_input_tokens,
            last_prompt_uncached_input_tokens:
              typeof data.last_prompt_uncached_input_tokens === 'number'
                ? data.last_prompt_uncached_input_tokens
                : data.last_prompt_uncached_input_tokens === null
                  ? null
                  : prev.last_prompt_uncached_input_tokens,
            last_prompt_cache_read_tokens:
              typeof data.last_prompt_cache_read_tokens === 'number'
                ? data.last_prompt_cache_read_tokens
                : data.last_prompt_cache_read_tokens === null
                  ? null
                  : prev.last_prompt_cache_read_tokens,
            last_prompt_cache_creation_tokens:
              typeof data.last_prompt_cache_creation_tokens === 'number'
                ? data.last_prompt_cache_creation_tokens
                : data.last_prompt_cache_creation_tokens === null
                  ? null
                  : prev.last_prompt_cache_creation_tokens,
            last_completion_output_tokens:
              typeof data.last_completion_output_tokens === 'number'
                ? data.last_completion_output_tokens
                : data.last_completion_output_tokens === null
                  ? null
                  : prev.last_completion_output_tokens,
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

    if (event === 'session_expired') {
      // Live -> archive transition can change the transcript backing store.
      void loadSessionDetail(updatedSessionId, {
        showLoading: false,
        clearOnError: false,
        errorMessage: 'Failed to refresh session metadata',
      })
      return
    }

    if (event === 'session_updated') {
      // Metadata-only refresh unless transcript identity changed — preserves
      // loaded older pages and scroll position.
      void refreshSessionMetadata(updatedSessionId)
    }
  }, [loadSessionDetail, refreshSessionMetadata, resetSessionDetail]))

  // Chat-backed sessions never set these counts (they stay 0), so this is false
  // for chat and true for transcript-backed sessions with older pages remaining.
  const hasMore = loadedCount < renderedTotal

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
    isLoadingOlder,
    firstItemIndex,
    transcriptDegradedReason,
    generateSummary,
    isGeneratingSummary,
  }
}
