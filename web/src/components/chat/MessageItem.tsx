import { memo } from 'react'
import type { ChatMessage } from '../../types/chat'
import { cn } from '../../lib/utils'
import { Markdown } from './Markdown'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCards, ToolChainGroup } from './ToolCallCard'
import type { A2UISurfaceState, UserAction } from '../canvas'

interface MessageItemProps {
  message: ChatMessage
  isStreaming?: boolean
  isThinking?: boolean
  onRespondToQuestion?: (toolCallId: string, answers: Record<string, string>) => void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

export const MessageItem = memo(function MessageItem({ message, isStreaming = false, isThinking = false, onRespondToQuestion, onRespondToApproval, canvasSurfaces, onCanvasInteraction }: MessageItemProps) {
  const isCommandResult = message.role === 'system' && message.toolCalls?.length && !message.content
  const isModelSwitch = message.role === 'system' && message.id.startsWith('model-switch-')

  // Don't render empty messages (e.g. compact acknowledgements with no body)
  const hasContent = message.content || message.thinkingContent ||
    (message.toolCalls && message.toolCalls.length > 0) ||
    (message.contentBlocks && message.contentBlocks.length > 0) ||
    isStreaming || isThinking
  if (!hasContent && !isModelSwitch) return null

  if (isModelSwitch) {
    return (
      <div className="flex justify-center py-2">
        <span className="text-xs text-muted-foreground bg-muted rounded-full px-3 py-1">
          {message.content}
        </span>
      </div>
    )
  }

  return (
    <div className={cn(
      'px-4 py-3',
      message.role === 'user' && 'bg-[#1e3a5f]/30',
      message.role === 'system' && !isCommandResult && 'bg-muted/30',
    )}>
      <div className="max-w-3xl mx-auto">
        <div className="flex items-center gap-2 mb-1.5">
          {message.role === 'assistant' && (
            <img src="/logo.png" alt="App logo" className="w-5 h-5 rounded" onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none' }} />
          )}
          <span className="text-xs font-medium text-muted-foreground">
            {message.role === 'user' ? 'You' : message.role === 'assistant' ? 'Gobby' : 'System'}
          </span>
          <span className="text-xs text-muted-foreground/60">
            {(() => {
              const date = message.timestamp instanceof Date ? message.timestamp : new Date(message.timestamp)
              return !isNaN(date.getTime()) ? date.toLocaleTimeString() : ''
            })()}
          </span>
        </div>

        {isThinking && !message.content && !message.thinkingContent && (
          <div className="flex items-center gap-2 py-2">
            <div className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin" />
            <span className="text-sm text-muted-foreground">Thinking...</span>
          </div>
        )}

        {message.thinkingContent !== undefined && (
          <ThinkingBlock content={message.thinkingContent} messageId={message.id} />
        )}

        {message.contentBlocks && message.contentBlocks.length > 0 ? (
          <>
            {message.contentBlocks.map((block, i) => {
              if (block.type === 'text') {
                const isLastText = !message.contentBlocks!.slice(i + 1).some(b => b.type === 'text')
                return (
                  <div key={`${message.id}-b${i}`} className="message-content text-sm leading-relaxed text-foreground">
                    <Markdown content={block.content} id={`${message.id}-${i}`} />
                    {isStreaming && isLastText && <span className="cursor inline-block w-2 h-4 bg-foreground animate-pulse ml-1.5" />}
                  </div>
                )
              }
              if (block.type === 'tool_chain') {
                return (
                  <ToolChainGroup
                    key={`${message.id}-b${i}`}
                    toolCalls={block.calls}
                    onRespond={onRespondToQuestion}
                    onRespondToApproval={onRespondToApproval}
                    canvasSurfaces={canvasSurfaces}
                    onCanvasInteraction={onCanvasInteraction}
                  />
                )
              }
              if (block.type === 'image') {
                return (
                  <div key={`${message.id}-b${i}`} className="my-2">
                    <img src={block.src} alt={block.alt || 'Image'} className="max-w-full rounded-lg border border-border" />
                  </div>
                )
              }
              return null
            })}
          </>
        ) : (
          <>
            {message.toolCalls && message.toolCalls.length > 0 && (
              <ToolCallCards toolCalls={message.toolCalls} onRespond={onRespondToQuestion} onRespondToApproval={onRespondToApproval} canvasSurfaces={canvasSurfaces} onCanvasInteraction={onCanvasInteraction} />
            )}
            {message.content && (
              <div className="message-content text-sm leading-relaxed text-foreground">
                <Markdown content={message.content} id={message.id} />
                {isStreaming && <span className="cursor inline-block w-2 h-4 bg-foreground animate-pulse ml-1.5" />}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
})
