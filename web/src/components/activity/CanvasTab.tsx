import { memo } from 'react'
import type { CanvasPanelState } from '../canvas/hooks/useCanvasPanel'

interface CanvasTabProps {
  state: CanvasPanelState | null
  onClose: () => void
  onClearAll?: () => void
}

export const CanvasTab = memo(function CanvasTab({ state, onClose, onClearAll }: CanvasTabProps) {
  if (!state) {
    return (
      <div className="activity-tab-empty">
        <div className="chat-empty-state">
          <div className="chat-empty-state__icon" aria-hidden="true">
            <CanvasEmptyIcon />
          </div>
          <div className="chat-empty-state__title">A2UI Canvas</div>
          <p className="chat-empty-state__copy">
            Interactive surfaces appear here when generated.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border">
        <span className="text-sm font-medium text-foreground truncate">
          {state.title || 'Canvas'}
        </span>
        <div className="flex items-center gap-2">
          {onClearAll && (
            <button
              className="text-muted-foreground hover:text-foreground text-xs"
              onClick={onClearAll}
            >
              Clear
            </button>
          )}
          <button
            className="text-muted-foreground hover:text-foreground text-xs"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-hidden relative">
        <iframe
          src={state.url}
          sandbox="allow-scripts"
          className="absolute inset-0 w-full h-full border-0 bg-white"
          title={state.title || 'Canvas'}
        />
      </div>
    </div>
  )
})

function CanvasEmptyIcon() {
  return (
    <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 19l7-7 3 3-7 7-3-3z" />
      <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z" />
      <path d="M2 2l7.586 7.586" />
      <circle cx="11" cy="11" r="2" />
    </svg>
  )
}
