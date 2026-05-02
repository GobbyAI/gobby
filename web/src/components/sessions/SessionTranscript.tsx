import { useMemo } from 'react'
import type { SessionMessage } from '../../hooks/useSessionDetail'
import { mapRenderedMessageToChatMessage } from '../../lib/chatMessageMapping'
import { MessageItem } from '../chat/MessageItem'

const SECTION_CLS = 'mb-4'
const HEADING_CLS =
  'mb-2 px-6 text-[length:var(--text-sm)] font-medium uppercase tracking-[0.03em] text-[var(--text-muted)]'
const MESSAGES_CLS = 'flex flex-col'
const LOADING_CLS = 'p-6 text-center text-[length:var(--text-base)] text-[var(--text-muted)]'

function mapToChatMessages(messages: SessionMessage[]) {
  return messages.map((message) => mapRenderedMessageToChatMessage(message))
}

interface SessionTranscriptProps {
  messages: SessionMessage[]
  totalMessages: number
  isLoading: boolean
}

export function SessionTranscript({
  messages,
  totalMessages,
  isLoading,
}: SessionTranscriptProps) {
  const chatMessages = useMemo(
    () => mapToChatMessages(messages),
    [messages],
  )

  return (
    <div className={SECTION_CLS}>
      <h3 className={HEADING_CLS}>Transcript ({totalMessages} messages)</h3>

      {isLoading && messages.length === 0 && (
        <div className={LOADING_CLS}>Loading messages...</div>
      )}

      <div className={MESSAGES_CLS}>
        {chatMessages.map((msg) => (
          <MessageItem key={msg.id} message={msg} />
        ))}
      </div>
    </div>
  )
}
