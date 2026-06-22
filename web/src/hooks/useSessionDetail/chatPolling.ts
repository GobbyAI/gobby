import { CHAT_MESSAGES_POLL_MS } from './constants'
import { fetchChatSessionMessages } from './api'
import type { MessageSource, SessionMessage } from './types'

interface Ref<T> {
  current: T
}

interface StartChatMessagePollingParams {
  activeSessionId: string
  isCurrent: () => boolean
  messageSourceRef: Ref<MessageSource>
  messagesRef: Ref<SessionMessage[]>
  setMessages: (messages: SessionMessage[]) => void
  setTotalMessages: (total: number) => void
}

export function startChatMessagePolling({
  activeSessionId,
  isCurrent,
  messageSourceRef,
  messagesRef,
  setMessages,
  setTotalMessages,
}: StartChatMessagePollingParams): () => void {
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

  return () => {
    cancelled = true
    if (pollTimeoutId !== null) {
      window.clearTimeout(pollTimeoutId)
    }
  }
}
