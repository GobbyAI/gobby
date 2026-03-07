import { useEffect, useMemo, useRef } from 'react'
import type { ChatMessage } from '../../types/chat'
import { ScrollArea } from './ui/ScrollArea'
import { MessageItem } from './MessageItem'
import { MessageErrorBoundary } from './MessageErrorBoundary'
import { PlanApprovalBar } from './PlanApprovalBar'
import type { A2UISurfaceState, UserAction } from '../canvas'

interface MessageListProps {
  messages: ChatMessage[]
  isStreaming: boolean
  isThinking: boolean
  isLoadingMessages?: boolean
  onRespondToQuestion?: (toolCallId: string, answers: Record<string, string>) => void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => void
  planPendingApproval?: boolean
  onApprovePlan?: () => void
  onRequestPlanChanges?: (feedback: string) => void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

export function MessageList({ messages, isStreaming, isThinking, isLoadingMessages, onRespondToQuestion, onRespondToApproval, planPendingApproval, onApprovePlan, onRequestPlanChanges, canvasSurfaces, onCanvasInteraction }: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const userScrolledUpRef = useRef(false)

  // Compute a fingerprint that changes when messages are added OR mutated
  // (e.g. tool_status updates that modify toolCalls on existing messages)
  const messageFingerprint = useMemo(() => messages.reduce((acc, m) => {
    const toolCount = m.toolCalls?.length ?? 0
    const lastStatus = m.toolCalls?.[toolCount - 1]?.status ?? ''
    const contentLen = m.content?.length ?? 0
    const blockCount = m.contentBlocks?.length ?? 0
    const thinkingLen = m.thinkingContent?.length ?? 0
    return acc + m.id + ':' + toolCount + ':' + lastStatus + ':' + contentLen + ':' + blockCount + ':' + thinkingLen + '|'
  }, ''), [messages])

  // Track whether user has manually scrolled away from bottom
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const handleScroll = () => {
      const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
      userScrolledUpRef.current = distanceFromBottom > 150
    }
    el.addEventListener('scroll', handleScroll, { passive: true })
    return () => el.removeEventListener('scroll', handleScroll)
  }, [])

  // Auto-scroll when content changes (new messages, tool updates, streaming)
  useEffect(() => {
    const el = scrollRef.current
    if (el && !userScrolledUpRef.current) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight
      })
    }
  }, [messageFingerprint, isThinking, isStreaming])

  return (
    <ScrollArea ref={scrollRef} className="flex-1 min-h-0 overflow-x-hidden">
      {messages.length === 0 && !isThinking ? (
        <div className="flex items-center justify-center h-full">
          <div className="text-center text-muted-foreground">
            {isLoadingMessages ? (
              <p className="text-sm animate-pulse">Loading messages...</p>
            ) : (
              <>
                <div className="text-lg mb-1">Chat</div>
                <p className="text-sm">Start a conversation with Gobby</p>
              </>
            )}
          </div>
        </div>
      ) : (
        <>
          {messages.map((message, i) => (
            <MessageErrorBoundary key={message.id} messageId={message.id}>
              <MessageItem
                message={message}
                isStreaming={isStreaming && i === messages.length - 1}
                isThinking={isThinking && i === messages.length - 1}
                onRespondToQuestion={onRespondToQuestion}
                onRespondToApproval={onRespondToApproval}
                canvasSurfaces={canvasSurfaces}
                onCanvasInteraction={onCanvasInteraction}
              />
            </MessageErrorBoundary>
          ))}
          {isThinking && (messages.length === 0 || messages[messages.length - 1].role === 'user') && (
            <ThinkingIndicator />
          )}
          {planPendingApproval && onApprovePlan && onRequestPlanChanges && (
            <PlanApprovalBar onApprove={onApprovePlan} onRequestChanges={onRequestPlanChanges} />
          )}
        </>
      )}
    </ScrollArea>
  )
}

function ThinkingIndicator() {
  return (
    <div className="px-4 py-3">
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-2 mb-1.5">
          <img src="/logo.png" alt="App logo" className="w-5 h-5 rounded" onError={(e) => { (e.target as HTMLImageElement).style.display = 'none' }} />
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
