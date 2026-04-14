import { useCallback } from 'react'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'

interface RunningAgent {
  run_id: string
  provider: string
  pid?: number
  mode?: string
  started_at?: string
  session_id?: string
}

interface CommandBarProps {
  sessionRef: string | null
  title: string | null
  onOpenPalette: () => void
  onOpenActiveSessions: () => void
  onNewChat: (agentName?: string) => void
  onTogglePanel: () => void
  agents: RunningAgent[]
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
  isPanelPinned: boolean
}

export function CommandBar({
  sessionRef,
  title,
  onOpenPalette,
  onOpenActiveSessions: _onOpenActiveSessions,
  onNewChat,
  onTogglePanel,
  agents: _agents,
  agentDefinitions: _agentDefinitions = [],
  agentGlobalDefs: _agentGlobalDefs = [],
  agentProjectDefs: _agentProjectDefs = [],
  agentShowScopeToggle: _agentShowScopeToggle = false,
  agentHasGlobal: _agentHasGlobal = false,
  agentHasProject: _agentHasProject = false,
  isPanelPinned,
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
          {sessionRef && (
            <span className="command-bar-ref">{sessionRef}</span>
          )}
          <span className="command-bar-title">
            {title ?? 'New Chat Session'}
          </span>
          <span className="command-bar-caret">&#9662;</span>
        </button>
      </div>

      {/* Right cluster — Live activity */}
      <div className="command-bar-right">
        <button
          type="button"
          className="command-bar-btn"
          onClick={handleNewChat}
          title="New Chat"
        >
          <PlusIcon />
        </button>

        <button
          type="button"
          className="command-bar-btn"
          onClick={onTogglePanel}
          title={isPanelPinned ? 'Unpin panel (Cmd+`)' : 'Pin panel (Cmd+`)'}
        >
          <PanelIcon pinned={isPanelPinned} />
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

function PanelIcon({ pinned }: { pinned: boolean }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="15" y1="3" x2="15" y2="21" />
      {pinned && <line x1="18" y1="9" x2="21" y2="9" opacity="0.5" />}
    </svg>
  )
}
