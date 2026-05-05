import { memo } from 'react'
import type { CanvasPanelState } from '../canvas/hooks/useCanvasPanel'
import { ActivityPanelEmpty, CanvasEmptyIcon } from './ActivityPanelEmpty'

interface CanvasTabProps {
  state: CanvasPanelState | null
  onClose: () => void
  onClearAll?: () => void
}

export const CanvasTab = memo(function CanvasTab({ state, onClose, onClearAll }: CanvasTabProps) {
  if (!state) {
    return (
      <ActivityPanelEmpty
        icon={<CanvasEmptyIcon />}
        heading="A2UI Canvas"
        body="Interactive surfaces appear here when generated"
      />
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

