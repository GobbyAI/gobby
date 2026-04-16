import { memo } from 'react'
import type { ChatMessage } from '../../types/chat'
import { cn } from '../../lib/utils'
import { Markdown } from './Markdown'
import { ThinkingBlock } from './ThinkingBlock'
import { ToolCallCards, ToolChainGroup } from './ToolCallCard'
import { UnknownBlockCard } from './UnknownBlockCard'
import type { A2UISurfaceState, UserAction } from '../canvas'
import { splitProtocolContent } from './protocolContent'

/** Replace [Image: ...] text descriptions with styled placeholders */
function renderImagePlaceholders(content: string): string {
  return content.replace(
    /\[Image: original (\d+)x(\d+), displayed at (\d+)x(\d+)[^\]]*\]/g,
    (_match, origW: string, origH: string) =>
      `\n\n> 🖼️ **Image** (${origW}×${origH})\n\n`,
  )
}

interface MessageItemProps {
  message: ChatMessage
  isStreaming?: boolean
  isThinking?: boolean
  onRespondToQuestion?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}

function ProtocolAwareText({
  content,
  id,
  isStreaming = false,
  onRespondToQuestion,
  onRespondToApproval,
  canvasSurfaces,
  onCanvasInteraction,
}: {
  content: string
  id: string
  isStreaming?: boolean
  onRespondToQuestion?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (toolCallId: string, decision: 'approve' | 'reject' | 'approve_always') => boolean | void
  canvasSurfaces?: Map<string, A2UISurfaceState>
  onCanvasInteraction?: (canvasId: string, action: UserAction) => void
}) {
  const segments = splitProtocolContent(content, id)
  const textSegmentIndexes = segments
    .map((segment, index) => (segment.type === 'text' ? index : -1))
    .filter((index) => index >= 0)
  const lastTextSegmentIndex = textSegmentIndexes[textSegmentIndexes.length - 1]

  return (
    <>
      {segments.map((segment, index) => {
        if (segment.type === 'text') {
          return (
            <div
              key={`${id}-t${index}`}
              className="message-content leading-relaxed text-foreground"
              data-testid="chat-message-content"
            >
              <Markdown content={renderImagePlaceholders(segment.content)} id={`${id}-${index}`} />
              {isStreaming && index === lastTextSegmentIndex && (
                <span className="cursor inline-block w-2 h-4 bg-foreground animate-pulse ml-1.5" />
              )}
            </div>
          )
        }

        return (
          <ToolChainGroup
            key={`${id}-p${index}`}
            toolCalls={[segment.call]}
            onRespond={onRespondToQuestion}
            onRespondToApproval={onRespondToApproval}
            canvasSurfaces={canvasSurfaces}
            onCanvasInteraction={onCanvasInteraction}
          />
        )
      })}
    </>
  )
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

        {message.thinkingContent !== undefined && !(message.contentBlocks && message.contentBlocks.length > 0) && (
          <ThinkingBlock content={message.thinkingContent} messageId={message.id} />
        )}

        {message.contentBlocks && message.contentBlocks.length > 0 ? (
          <>
            {message.contentBlocks.map((block, i) => {
              if (block.type === 'text') {
                return (
                  <ProtocolAwareText
                    key={`${message.id}-b${i}`}
                    content={block.content}
                    id={`${message.id}-${i}`}
                    isStreaming={isStreaming}
                    onRespondToQuestion={onRespondToQuestion}
                    onRespondToApproval={onRespondToApproval}
                    canvasSurfaces={canvasSurfaces}
                    onCanvasInteraction={onCanvasInteraction}
                  />
                )
              }
              if (block.type === 'thinking') {
                return <ThinkingBlock key={`${message.id}-b${i}`} content={block.content} messageId={message.id} />
              }
              if (block.type === 'tool_chain') {
                return (
                  <ToolChainGroup
                    key={`${message.id}-b${i}`}
                    toolCalls={block.tool_calls}
                    onRespond={onRespondToQuestion}
                    onRespondToApproval={onRespondToApproval}
                    canvasSurfaces={canvasSurfaces}
                    onCanvasInteraction={onCanvasInteraction}
                  />
                )
              }
              if (block.type === 'tool_reference') {
                return (
                  <div key={`${message.id}-b${i}`} className="my-1 text-xs text-muted-foreground italic">
                    Referencing tool: {block.tool_name} ({block.server_name})
                  </div>
                )
              }
              if (block.type === 'image') {
                const { source } = block
                const src = source?.data ? `data:${source.media_type};base64,${source.data}` : ''
                if (!src) return null
                return (
                  <div key={`${message.id}-b${i}`} className="my-2">
                    <img src={src} alt="Image content" className="max-w-full rounded-lg border border-border" />
                  </div>
                )
              }
              if (block.type === 'document') {
                return (
                  <div key={`${message.id}-b${i}`} className="my-1 p-2 rounded bg-muted/50 border border-border text-xs flex items-center gap-2">
                    <span className="font-medium text-foreground">Document:</span>
                    <span className="truncate">{block.source?.name || 'Unknown'}</span>
                  </div>
                )
              }
              if (block.type === 'web_search_result') {
                return (
                  <div key={`${message.id}-b${i}`} className="my-1 p-2 rounded bg-accent/10 border border-accent/20 text-xs italic">
                    Search result included.
                  </div>
                )
              }
              if (block.type === 'unknown') {
                return (
                  <UnknownBlockCard
                    key={`${message.id}-b${i}`}
                    blockType={block.block_type}
                    raw={block.raw}
                  />
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
              <ProtocolAwareText
                content={message.content}
                id={message.id}
                isStreaming={isStreaming}
                onRespondToQuestion={onRespondToQuestion}
                onRespondToApproval={onRespondToApproval}
                canvasSurfaces={canvasSurfaces}
                onCanvasInteraction={onCanvasInteraction}
              />
            )}
          </>
        )}
      </div>
    </div>
  )
})
