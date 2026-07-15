import { useState, useEffect, useCallback, useRef } from 'react'
import type { GobbySession } from '../types/sessions'
import {
  START_INDEX,
  applyTailRefreshTranscriptPage,
  appendNewerTranscriptPage,
  createEmptyTranscriptWindow,
  createTailTranscriptWindow,
  prependOlderTranscriptPage,
  type TranscriptWindowState,
  type TranscriptWindowUpdate,
} from './sessionTranscriptWindow'
import {
  fetchChatSessionMessages,
  fetchRenderedSessionMessages,
  fetchSessionMetadata,
  fetchTranscriptStatus,
  toTranscriptWindowPage,
} from './useSessionDetail/api'
import { startChatMessagePolling } from './useSessionDetail/chatPolling'
import {
  SESSION_METADATA_UNAVAILABLE_MESSAGE,
  TAIL_REFRESH_DEBOUNCE_MS,
} from './useSessionDetail/constants'
import { useSessionDetailRealtimeEvents } from './useSessionDetail/realtimeEvents'
import { useSessionSummaryActions } from './useSessionDetail/summaryActions'
import type {
  LoadSessionDetailOptions,
  MessageLoadResult,
  MessageSource,
  SessionMessage,
  TranscriptStatus,
} from './useSessionDetail/types'
import { useSessionUsageEvents } from './useSessionDetail/usageEvents'

export type { SessionMessage, TranscriptStatus } from './useSessionDetail/types'

export function useSessionDetail(sessionId: string | null) {
  const [session, setSession] = useState<GobbySession | null>(null)
  const [messages, setMessages] = useState<SessionMessage[]>([])
  const [transcriptStatus, setTranscriptStatus] = useState<TranscriptStatus | null>(null)
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [messageSource, setMessageSourceState] = useState<MessageSource>(null)
  const [totalMessages, setTotalMessages] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  // Rendered-group pagination state (group counts, not parsed-message counts).
  const [windowStart, setWindowStart] = useState(0)
  const [windowEnd, setWindowEnd] = useState(0)
  const [renderedTotal, setRenderedTotal] = useState(0)
  const [isLoadingOlder, setIsLoadingOlder] = useState(false)
  const [isLoadingNewer, setIsLoadingNewer] = useState(false)
  const [firstItemIndex, setFirstItemIndex] = useState(START_INDEX)
  const [transcriptDegradedReason, setTranscriptDegradedReason] = useState<string | null>(null)

  const messageSourceRef = useRef<MessageSource>(null)
  const messagesRef = useRef<SessionMessage[]>([])
  const transcriptWindowRef = useRef<TranscriptWindowState<SessionMessage>>(
    createEmptyTranscriptWindow<SessionMessage>(),
  )
  const sessionIdRef = useRef(sessionId)
  const sessionRef = useRef<GobbySession | null>(null)
  const detailPollCleanupRef = useRef<(() => void) | null>(null)
  const tailRefreshTimeoutRef = useRef<number | null>(null)
  const tailRefreshInFlightRef = useRef(false)
  const tailRefreshPendingSessionRef = useRef<string | null>(null)
  const scheduleTailRefreshRef = useRef<(activeSessionId: string) => void>(() => {})
  const tailWindowVersionRef = useRef(0)
  const detailLoadVersionRef = useRef(0)
  const transcriptAtBottomRef = useRef(true)
  const loadingOlderRef = useRef(false)
  const loadingNewerRef = useRef(false)

  useEffect(() => {
    sessionIdRef.current = sessionId
  }, [sessionId])

  useEffect(() => {
    sessionRef.current = session
  }, [session])

  const clearDetailPolling = useCallback(() => {
    detailPollCleanupRef.current?.()
    detailPollCleanupRef.current = null
  }, [])

  const clearTailRefresh = useCallback(() => {
    if (tailRefreshTimeoutRef.current !== null) {
      window.clearTimeout(tailRefreshTimeoutRef.current)
      tailRefreshTimeoutRef.current = null
    }
    tailRefreshPendingSessionRef.current = null
  }, [])

  const setMessageSource = useCallback((source: MessageSource) => {
    messageSourceRef.current = source
    setMessageSourceState(source)
  }, [])

  const applyTranscriptWindow = useCallback((
    update: TranscriptWindowState<SessionMessage> | TranscriptWindowUpdate<SessionMessage>,
  ) => {
    const nextWindow = 'state' in update ? update.state : update
    transcriptWindowRef.current = nextWindow
    messagesRef.current = nextWindow.messages
    tailWindowVersionRef.current += 1
    setMessages(nextWindow.messages)
    setWindowStart(nextWindow.windowStart)
    setWindowEnd(nextWindow.windowEnd)
    setRenderedTotal(nextWindow.renderedTotal)
    setFirstItemIndex(nextWindow.firstItemIndex)
  }, [])

  const resetPaging = useCallback(() => {
    const emptyWindow = createEmptyTranscriptWindow<SessionMessage>()
    transcriptWindowRef.current = emptyWindow
    messagesRef.current = emptyWindow.messages
    setMessages(emptyWindow.messages)
    setWindowStart(0)
    setWindowEnd(0)
    setRenderedTotal(0)
    loadingOlderRef.current = false
    loadingNewerRef.current = false
    transcriptAtBottomRef.current = true
    tailRefreshInFlightRef.current = false
    tailRefreshPendingSessionRef.current = null
    tailWindowVersionRef.current += 1
    setIsLoadingOlder(false)
    setIsLoadingNewer(false)
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
    setMessageSource(null)
    resetPaging()
  }, [resetPaging, setMessageSource])

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
    }: LoadSessionDetailOptions = {},
  ) => {
    const loadVersion = detailLoadVersionRef.current + 1
    detailLoadVersionRef.current = loadVersion
    clearTailRefresh()
    const isCurrent = () =>
      sessionIdRef.current === activeSessionId && detailLoadVersionRef.current === loadVersion

    if (showLoading) {
      resetPaging()
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

        setMessageSource('chat')
        setTranscriptStatus(null)
        messagesRef.current = chatResult.mapped
        setMessages(chatResult.mapped)
        setTotalMessages(chatResult.totalCount)

        detailPollCleanupRef.current = startChatMessagePolling({
          activeSessionId,
          isCurrent,
          messageSourceRef,
          messagesRef,
          setMessages,
          setTotalMessages,
        })
        return
      }

      setMessageSource('session')
      const initialWindow = createTailTranscriptWindow(
        toTranscriptWindowPage(renderedResult),
      )
      applyTranscriptWindow(initialWindow)
      setTotalMessages(renderedResult.totalCount)
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
  }, [
    applyClearedDetail,
    applyTranscriptWindow,
    clearDetailPolling,
    clearTailRefresh,
    resetPaging,
    setMessageSource,
  ])

  const applyRefreshedTailMessages = useCallback((result: MessageLoadResult) => {
    if (loadingOlderRef.current || loadingNewerRef.current) return

    const update = applyTailRefreshTranscriptPage(
      transcriptWindowRef.current,
      toTranscriptWindowPage(result),
      transcriptAtBottomRef.current,
    )
    if (update.changed) {
      applyTranscriptWindow(update)
    }
    setTotalMessages((prev) => Math.max(result.totalCount, prev + update.addedCount))
    setTranscriptDegradedReason(result.degradedReason)
    if (update.state.messages.length > 0) {
      setTranscriptStatus(null)
    }
  }, [applyTranscriptWindow])

  const scheduleTailRefresh = useCallback((activeSessionId: string) => {
    if (messageSourceRef.current !== 'session') return

    clearTailRefresh()
    tailRefreshTimeoutRef.current = window.setTimeout(() => {
      tailRefreshTimeoutRef.current = null
      if (sessionIdRef.current !== activeSessionId || messageSourceRef.current !== 'session') return
      if (tailRefreshInFlightRef.current) {
        tailRefreshPendingSessionRef.current = activeSessionId
        return
      }
      if (loadingOlderRef.current || loadingNewerRef.current) return

      const refreshVersion = detailLoadVersionRef.current
      const tailWindowVersion = tailWindowVersionRef.current
      tailRefreshInFlightRef.current = true
      void (async () => {
        try {
          const result = await fetchRenderedSessionMessages(activeSessionId, 0, 'tail')
          if (
            sessionIdRef.current !== activeSessionId ||
            detailLoadVersionRef.current !== refreshVersion ||
            tailWindowVersionRef.current !== tailWindowVersion ||
            loadingOlderRef.current ||
            loadingNewerRef.current ||
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
        } finally {
          tailRefreshInFlightRef.current = false
          const pendingSessionId = tailRefreshPendingSessionRef.current
          tailRefreshPendingSessionRef.current = null
          if (
            pendingSessionId &&
            sessionIdRef.current === pendingSessionId &&
            detailLoadVersionRef.current === refreshVersion &&
            messageSourceRef.current === 'session'
          ) {
            scheduleTailRefreshRef.current(pendingSessionId)
          }
        }
      })()
    }, TAIL_REFRESH_DEBOUNCE_MS)
  }, [applyRefreshedTailMessages, clearTailRefresh])

  useEffect(() => {
    scheduleTailRefreshRef.current = scheduleTailRefresh
  }, [scheduleTailRefresh])

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
    if (loadingOlderRef.current || loadingNewerRef.current) return

    const currentWindow = transcriptWindowRef.current
    if (currentWindow.windowStart <= 0) return

    const loadVersion = detailLoadVersionRef.current
    const windowVersion = tailWindowVersionRef.current
    const requestedOffset = currentWindow.renderedTotal - currentWindow.windowStart
    if (requestedOffset <= 0) return

    loadingOlderRef.current = true
    setIsLoadingOlder(true)
    try {
      const result = await fetchRenderedSessionMessages(
        activeSessionId,
        requestedOffset,
        'tail',
      )
      if (
        sessionIdRef.current !== activeSessionId ||
        detailLoadVersionRef.current !== loadVersion ||
        tailWindowVersionRef.current !== windowVersion
      ) {
        return
      }
      if (!result.ok) return

      const update = prependOlderTranscriptPage(
        transcriptWindowRef.current,
        toTranscriptWindowPage(result),
      )
      if (update.changed) {
        applyTranscriptWindow(update)
      }
      setTotalMessages((prev) => Math.max(prev, result.totalCount))
      if (result.degradedReason) {
        setTranscriptDegradedReason(result.degradedReason)
      }
    } finally {
      loadingOlderRef.current = false
      if (sessionIdRef.current === activeSessionId) {
        setIsLoadingOlder(false)
      }
    }
  }, [applyTranscriptWindow])

  const loadNewer = useCallback(async () => {
    const activeSessionId = sessionIdRef.current
    if (!activeSessionId) return
    if (messageSourceRef.current !== 'session') return
    if (loadingOlderRef.current || loadingNewerRef.current) return

    const currentWindow = transcriptWindowRef.current
    if (currentWindow.windowEnd >= currentWindow.renderedTotal) return

    const loadVersion = detailLoadVersionRef.current
    const windowVersion = tailWindowVersionRef.current
    const requestedOffset = currentWindow.windowEnd

    loadingNewerRef.current = true
    setIsLoadingNewer(true)
    try {
      const result = await fetchRenderedSessionMessages(
        activeSessionId,
        requestedOffset,
        'head',
      )
      if (
        sessionIdRef.current !== activeSessionId ||
        detailLoadVersionRef.current !== loadVersion ||
        tailWindowVersionRef.current !== windowVersion
      ) {
        return
      }
      if (!result.ok) return

      const update = appendNewerTranscriptPage(
        transcriptWindowRef.current,
        toTranscriptWindowPage(result),
      )
      if (update.changed) {
        applyTranscriptWindow(update)
      }
      setTotalMessages((prev) => Math.max(prev, result.totalCount))
      if (result.degradedReason) {
        setTranscriptDegradedReason(result.degradedReason)
      }
    } finally {
      loadingNewerRef.current = false
      if (sessionIdRef.current === activeSessionId) {
        setIsLoadingNewer(false)
      }
    }
  }, [applyTranscriptWindow])

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

  useSessionDetailRealtimeEvents({
    sessionIdRef,
    transcriptWindowRef,
    transcriptAtBottomRef,
    setMessageSource,
    applyTranscriptWindow,
    loadNewer,
    setTotalMessages,
    setTranscriptStatus,
    resetSessionDetail,
    loadSessionDetail,
    refreshSessionMetadata,
  })
  useSessionUsageEvents({ sessionIdRef, setSession })

  // Chat-backed sessions never set these counts (they stay 0), so this is false
  // for chat and true for transcript-backed sessions with older pages remaining.
  const hasMore = messageSource === 'session' && windowStart > 0
  const hasNewer = messageSource === 'session' && windowEnd < renderedTotal

  const setTranscriptAtBottom = useCallback((atBottom: boolean) => {
    transcriptAtBottomRef.current = atBottom
  }, [])

  const { generateSummary, isGeneratingSummary } = useSessionSummaryActions(sessionId, setSession)

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
    hasNewer,
    loadNewer,
    isLoadingOlder,
    isLoadingNewer,
    setTranscriptAtBottom,
    firstItemIndex,
    transcriptDegradedReason,
    generateSummary,
    isGeneratingSummary,
  }
}
