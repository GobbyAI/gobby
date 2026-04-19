import { useCallback } from 'react'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { getSessionTitleText } from '../../lib/sessionTitle'
import { SourceIcon } from '../shared/SourceIcon'

interface CommandBarProps {
  sessionRef: string | null
  title: string | null
  sessionSource?: string | null
  onOpenPalette: () => void
  onNewChat: (agentName?: string) => void
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
  onNewChat,
  agentDefinitions: _agentDefinitions = [],
  agentGlobalDefs: _agentGlobalDefs = [],
  agentProjectDefs: _agentProjectDefs = [],
  agentShowScopeToggle: _agentShowScopeToggle = false,
  agentHasGlobal: _agentHasGlobal = false,
  agentHasProject: _agentHasProject = false,
}: CommandBarProps) {
  const handleNewChat = useCallback(() => {
    onNewChat()
  }, [onNewChat])

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
          <span className="command-bar-caret">&#9662;</span>
        </button>
      </div>

      {/* Right cluster — Actions */}
      <div className="command-bar-right">
        <button
          type="button"
          className="command-bar-btn"
          onClick={handleNewChat}
          title="New Chat"
        >
          <PlusIcon />
        </button>
      </div>
    </div>
  )
}

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" clipRule="evenodd" fillRule="evenodd">
      <path d="M8 1a1 1 0 0 1 1 1v5h5a1 1 0 1 1 0 2H9v5a1 1 0 1 1-2 0V9H2a1 1 0 0 1 0-2h5V2a1 1 0 0 1 1-1Z" />
    </svg>
  )
}
