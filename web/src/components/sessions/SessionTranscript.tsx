import { useMemo } from 'react'
import type { SessionMessage } from '../../hooks/useSessionDetail'
import { mapRenderedMessageToChatMessage } from '../../lib/chatMessageMapping'
import { MessageItem } from '../chat/MessageItem'

/** Map SessionMessages (RenderedMessage shape) to ChatMessages for rendering. */
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
    <div className="session-transcript">
      <h3>Transcript ({totalMessages} messages)</h3>

      {isLoading && messages.length === 0 && (
        <div className="session-transcript-loading">Loading messages...</div>
      )}

      <div className="session-transcript-messages">
        {chatMessages.map((msg) => (
          <MessageItem key={msg.id} message={msg} />
        ))}
      </div>
    </div>
  )
}
