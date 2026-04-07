import { useState, useCallback } from 'react'
import type { SessionObservationMeta } from '../../types/chat'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { AgentPickerDropdown } from './AgentPickerDropdown'

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
  viewingMeta?: SessionObservationMeta | null
  isAttached?: boolean
  onAttach?: () => void
  onDetach?: () => void
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

const SOURCE_CONFIG: Record<string, { label: string; dot: string }> = {
  claude: { label: 'Claude', dot: 'bg-purple-400' },
  gemini: { label: 'Gemini', dot: 'bg-green-400' },
  codex: { label: 'Codex', dot: 'bg-blue-400' },
}

export function CommandBar({
  sessionRef,
  title,
  viewingMeta,
  isAttached,
  onAttach,
  onDetach,
  onOpenPalette,
  onOpenActiveSessions: _onOpenActiveSessions,
  onNewChat,
  onTogglePanel,
  agents: _agents,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
  isPanelPinned,
}: CommandBarProps) {
  const [showAgentPicker, setShowAgentPicker] = useState(false)
  const isObserving = !!viewingMeta

  const handleNewChat = useCallback(() => {
    if (agentDefinitions.length <= 1) {
      onNewChat()
    } else {
      setShowAgentPicker(true)
    }
  }, [agentDefinitions.length, onNewChat])

  return (
    <div className="command-bar">
      {/* Left cluster — Session context */}
      <div className="command-bar-left">
        {isObserving && viewingMeta ? (
          <ObservationSegment
            sessionRef={sessionRef}
            viewingMeta={viewingMeta}
            isAttached={!!isAttached}
            onAttach={onAttach}
            onDetach={onDetach}
          />
        ) : (
          <button
            type="button"
            className="command-bar-session"
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
        )}
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

        {showAgentPicker && (
          <AgentPickerDropdown
            definitions={agentDefinitions}
            globalDefs={agentGlobalDefs}
            projectDefs={agentProjectDefs}
            showScopeToggle={agentShowScopeToggle}
            hasGlobal={agentHasGlobal}
            hasProject={agentHasProject}
            onSelect={(name) => {
              onNewChat(name)
              setShowAgentPicker(false)
            }}
            onClose={() => setShowAgentPicker(false)}
          />
        )}

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

function ObservationSegment({
  sessionRef,
  viewingMeta,
  isAttached,
  onAttach,
  onDetach,
}: {
  sessionRef: string | null
  viewingMeta: SessionObservationMeta
  isAttached: boolean
  onAttach?: () => void
  onDetach?: () => void
}) {
  const sourceConf = SOURCE_CONFIG[viewingMeta.source]
  const sourceLabel = sourceConf?.label ?? viewingMeta.source
  const sourceDot = sourceConf?.dot ?? 'bg-neutral-400'
  const isLive = viewingMeta.status === 'active'

  return (
    <div className="command-bar-observation">
      {sessionRef && (
        <span className="command-bar-ref">{sessionRef}</span>
      )}
      <span className="command-bar-title">
        {viewingMeta.title ?? 'Terminal session'}
      </span>
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${sourceDot}`} />
      <span className="text-muted-foreground text-[11px]">{sourceLabel}</span>
      <span className="text-muted-foreground/60 text-[11px]">&middot;</span>
      <span className="text-muted-foreground text-[11px]">
        {isAttached ? 'Attached' : 'Observing'}
        {isLive && !isAttached && ' (live)'}
      </span>
      {(() => {
        if (isAttached && onDetach) return <button className="command-bar-obs-btn" onClick={onDetach}>Detach</button>
        if (!isAttached && onAttach) return <button className="command-bar-obs-btn command-bar-obs-btn--attach" onClick={onAttach}>Attach</button>
        return null
      })()}
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
