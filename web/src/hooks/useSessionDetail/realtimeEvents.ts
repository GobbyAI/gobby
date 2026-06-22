import { useCallback, type Dispatch, type SetStateAction } from 'react'
import {
  applyLiveTranscriptMessage,
  type TranscriptWindowState,
  type TranscriptWindowUpdate,
} from '../sessionTranscriptWindow'
import { useWebSocketEvent } from '../useWebSocketEvent'
import { mapRenderedRecordToSessionMessage } from './api'
import { SESSION_METADATA_UNAVAILABLE_MESSAGE } from './constants'
import type {
  LoadSessionDetailOptions,
  MessageSource,
  SessionMessage,
  TranscriptStatus,
} from './types'

interface Ref<T> {
  current: T
}

interface UseSessionDetailRealtimeEventsParams {
  sessionIdRef: Ref<string | null>
  transcriptWindowRef: Ref<TranscriptWindowState<SessionMessage>>
  transcriptAtBottomRef: Ref<boolean>
  setMessageSource: (source: MessageSource) => void
  applyTranscriptWindow: (
    update: TranscriptWindowState<SessionMessage> | TranscriptWindowUpdate<SessionMessage>,
  ) => void
  loadNewer: () => Promise<void>
  setTotalMessages: Dispatch<SetStateAction<number>>
  setTranscriptStatus: Dispatch<SetStateAction<TranscriptStatus | null>>
  resetSessionDetail: (error: string | null) => void
  loadSessionDetail: (
    activeSessionId: string,
    options?: LoadSessionDetailOptions,
  ) => Promise<void>
  refreshSessionMetadata: (activeSessionId: string) => Promise<void>
}

export function useSessionDetailRealtimeEvents({
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
}: UseSessionDetailRealtimeEventsParams): void {
  useWebSocketEvent('session_message', useCallback((data: Record<string, unknown>) => {
    const msgSessionId = data.session_id as string | undefined
    if (!msgSessionId || msgSessionId !== sessionIdRef.current) return

    const msg = data.message as Record<string, unknown> | undefined
    if (!msg) return

    setMessageSource('session')
    const newMessage = mapRenderedRecordToSessionMessage(msg)
    const update = applyLiveTranscriptMessage(
      transcriptWindowRef.current,
      newMessage,
      transcriptAtBottomRef.current,
    )

    if (update.changed) {
      applyTranscriptWindow(update)
    }
    if (update.needsFetch) {
      void loadNewer()
    }
    if (update.addedCount > 0) {
      setTotalMessages((prev) => prev + update.addedCount)
    }
    if (update.state.messages.length > 0) {
      setTranscriptStatus(null)
    }
  }, [
    applyTranscriptWindow,
    loadNewer,
    sessionIdRef,
    setMessageSource,
    setTotalMessages,
    setTranscriptStatus,
    transcriptAtBottomRef,
    transcriptWindowRef,
  ]))

  useWebSocketEvent('session_event', useCallback((data: Record<string, unknown>) => {
    const event = typeof data.event === 'string' ? data.event : null
    const updatedSessionId = typeof data.session_id === 'string' ? data.session_id : null
    if (!updatedSessionId || updatedSessionId !== sessionIdRef.current) return

    if (event === 'session_deleted') {
      resetSessionDetail(SESSION_METADATA_UNAVAILABLE_MESSAGE)
      return
    }

    if (event === 'session_expired') {
      void loadSessionDetail(updatedSessionId, {
        showLoading: false,
        clearOnError: false,
        errorMessage: 'Failed to refresh session metadata',
      })
      return
    }

    if (event === 'session_updated') {
      void refreshSessionMetadata(updatedSessionId)
    }
  }, [
    loadSessionDetail,
    refreshSessionMetadata,
    resetSessionDetail,
    sessionIdRef,
  ]))
}
