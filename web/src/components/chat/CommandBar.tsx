import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { getSessionTitleText } from '../../lib/sessionTitle'
import { SourceIcon } from '../shared/SourceIcon'
import { PanelIcon } from './icons/PanelIcon'

interface CommandBarProps {
  sessionRef: string | null
  title: string | null
  sessionSource?: string | null
  onOpenPalette: () => void
  onTogglePanel?: () => void
  isPanelPinned?: boolean
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
}

export function CommandBar({
  sessionRef,
  title,
  sessionSource,
  onOpenPalette,
  onTogglePanel,
  isPanelPinned = false,
  agentDefinitions: _agentDefinitions = [],
  agentGlobalDefs: _agentGlobalDefs = [],
  agentProjectDefs: _agentProjectDefs = [],
  agentShowScopeToggle: _agentShowScopeToggle = false,
  agentHasGlobal: _agentHasGlobal = false,
  agentHasProject: _agentHasProject = false,
}: CommandBarProps) {
  return (
    <div className="command-bar">
      {/* Left cluster — Session context */}
      <div className="command-bar-left">
        <button
          type="button"
          className="command-bar-session"
          data-testid="chat-session-selector"
          onClick={onOpenPalette}
          title="Switch session (Cmd+K)"
        >
          {sessionSource && (
            <span className="command-bar-source" aria-hidden="true">
              <SourceIcon source={sessionSource} size={14} />
            </span>
          )}
          {sessionRef && (
            <span className="command-bar-ref">{sessionRef}</span>
          )}
          <span className="command-bar-title">
            {getSessionTitleText(title)}
          </span>
          <ChevronDownIcon />
        </button>
      </div>

      {/* Right cluster — Actions */}
      <div className="command-bar-right">
        {onTogglePanel && (
          <button
            type="button"
            className="command-bar-btn"
            onClick={onTogglePanel}
            aria-label={isPanelPinned ? 'Hide activity panel' : 'Show activity panel'}
            title={isPanelPinned ? 'Hide activity panel' : 'Show activity panel'}
          >
            <PanelIcon pinned={isPanelPinned} />
          </button>
        )}
      </div>
    </div>
  )
}

function ChevronDownIcon() {
  return (
    <svg
      className="command-bar-caret"
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="4 6 8 10 12 6" />
    </svg>
  )
}
