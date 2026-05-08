import { useCallback, useEffect, useImperativeHandle, useRef, forwardRef } from 'react'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import type { ChatMessage } from '../../types/chat'
import { MessageItem } from './MessageItem'
import { MessageErrorBoundary } from './MessageErrorBoundary'
import type { A2UISurfaceState, UserAction } from '../canvas'
import { GobbyLogo } from '../shared/GobbyLogo'

interface MessageListProps {
  messages: ChatMessage[]
  isStreaming: boolean
  isThinking: boolean
  isLoadingMessages?: boolean
  onRespondToQuestion?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

export interface MessageListHandle {
  scrollToBottom: () => void
}

export const MessageList = forwardRef<MessageListHandle, MessageListProps>(function MessageList({ messages, isStreaming, isThinking, isLoadingMessages, onRespondToQuestion, onRespondToApproval, canvasSurfaces, onCanvasInteraction }, ref) {
  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const userScrolledUpRef = useRef(false)

  useImperativeHandle(ref, () => ({
    scrollToBottom() {
      userScrolledUpRef.current = false
      virtuosoRef.current?.scrollToIndex({ index: 'LAST', behavior: 'smooth' })
    },
  }))

  const handleAtBottomStateChange = useCallback((atBottom: boolean) => {
    // Don't flip the flag during streaming — content growth can briefly
    // push us past atBottomThreshold before followOutput scrolls back,
    // which causes the "bounce" where auto-scroll stops mid-stream.
    if (!isStreaming) {
      userScrolledUpRef.current = !atBottom
    }
  }, [isStreaming])

  // Reset scroll flag when streaming starts so stale scroll-up state
  // from before the agent began doesn't prevent auto-scroll.
  useEffect(() => {
    if (isStreaming) {
      userScrolledUpRef.current = false
    }
  }, [isStreaming])

  // Scroll to bottom on non-streaming appends (e.g. loading history).
  // During streaming, Virtuoso's followOutput handles scroll-following
  // natively — adding manual scrollToIndex calls here causes bounce
  // because they compete with followOutput's own smooth scroll.
  useEffect(() => {
    if (!isStreaming && !userScrolledUpRef.current && messages.length > 0) {
      virtuosoRef.current?.scrollToIndex({ index: 'LAST', behavior: 'smooth' })
    }
  }, [messages.length, isStreaming])

  const Footer = useCallback(() => (
    <>
      {isThinking && (messages.length === 0 || messages[messages.length - 1].role === 'user') && (
        <ThinkingIndicator />
      )}
    </>
  ), [isThinking, messages])

  // Stable reference for itemContent to avoid Virtuoso re-renders
  const itemContent = useCallback((index: number, message: ChatMessage) => (
    <MessageErrorBoundary key={message.id} messageId={message.id}>
      <MessageItem
        message={message}
        isStreaming={isStreaming && index === messages.length - 1}
        isThinking={isThinking && index === messages.length - 1}
        onRespondToQuestion={onRespondToQuestion}
        onRespondToApproval={onRespondToApproval}
        canvasSurfaces={canvasSurfaces}
        onCanvasInteraction={onCanvasInteraction}
      />
    </MessageErrorBoundary>
  ), [isStreaming, isThinking, messages.length, onRespondToQuestion, onRespondToApproval, canvasSurfaces, onCanvasInteraction])

  if (messages.length === 0 && !isThinking) {
    return (
      <div className="chat-scaled flex-1 min-h-0 flex items-center justify-center">
        <div className="chat-empty-state">
          {isLoadingMessages ? (
            <p className="text-sm animate-pulse">Loading messages...</p>
          ) : (
            <>
              <div className="chat-empty-state__icon" aria-hidden="true">
                <ChatEmptyIcon />
              </div>
              <div className="chat-empty-state__title">Chat</div>
              <p className="chat-empty-state__copy">Start a conversation with Gobby.</p>
            </>
          )}
        </div>
      </div>
    )
  }

  return (
    <Virtuoso
      ref={virtuosoRef}
      className="chat-scaled flex-1 min-h-0 overflow-x-hidden [&::-webkit-scrollbar]:w-2 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [scrollbar-width:thin] [scrollbar-color:var(--border)_transparent]"
      data={messages}
      itemContent={itemContent}
      followOutput={() => {
        // Always follow during streaming — the agent is working and
        // the viewport should stay pinned to the bottom.
        if (isStreaming) return 'smooth'
        if (userScrolledUpRef.current) return false
        return 'smooth'
      }}
      atBottomThreshold={400}
      atBottomStateChange={handleAtBottomStateChange}
      overscan={400}
      increaseViewportBy={200}
      components={{ Footer }}
    />
  )
})

function ChatEmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="opacity-30">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function ThinkingIndicator() {
  return (
    <div className="px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-2 mb-1.5">
          <GobbyLogo label="App logo" className="rounded" />
          <span className="text-xs font-medium text-muted-foreground">Gobby</span>
        </div>
        <div className="flex items-center gap-2 py-2">
          <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
          <span className="text-sm text-muted-foreground">Thinking...</span>
        </div>
      </div>
    </div>
  )
}
