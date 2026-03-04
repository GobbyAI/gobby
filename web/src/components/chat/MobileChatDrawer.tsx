import { useState } from 'react'
import type { GobbySession } from '../../hooks/useSessions'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { formatRelativeTime } from '../../utils/formatTime'
import { AgentPickerDropdown } from './AgentPickerDropdown'

interface MobileChatDrawerProps {
  sessions: GobbySession[]
  activeSessionId: string | null
  sessionRef: string | null
  title: string | null
  onNewChat: (agentName?: string) => void
  onSelectSession: (session: GobbySession) => void
  onDeleteSession?: (session: GobbySession) => void
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
}

export function MobileChatDrawer({
  sessions,
  activeSessionId,
  sessionRef,
  title,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
}: MobileChatDrawerProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [showAgentPicker, setShowAgentPicker] = useState(false)

  const activeSession = sessions.find((s) => s.external_id === activeSessionId)

  const handleNewChat = () => {
    if (agentDefinitions.length <= 1) {
      onNewChat()
      setIsOpen(false)
    } else {
      setShowAgentPicker(true)
    }
  }

  return (
    <div className={`mobile-chat-drawer ${isOpen ? '' : 'collapsed'}`}>
      <div
        className="mobile-chat-drawer-header"
        onClick={() => setIsOpen(!isOpen)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setIsOpen(!isOpen) } }}
      >
        <span className="mobile-chat-drawer-title">
          <ChatsIcon />
          {isOpen ? 'Chats' : (
            <>
              {sessionRef && <span className="font-mono text-accent">{sessionRef}</span>}
              {sessionRef && title ? ': ' : ''}
              <span className="truncate">{!isOpen && !title ? 'New conversation' : title}</span>
            </>
          )}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          {!isOpen && onDeleteSession && activeSession && (
            <button
              type="button"
              className="mobile-drawer-action"
              onClick={(e) => { e.stopPropagation(); onDeleteSession(activeSession) }}
              title="Delete chat"
            >
              <TrashIcon />
            </button>
          )}
          <button
            type="button"
            className="mobile-drawer-action"
            onClick={(e) => { e.stopPropagation(); handleNewChat() }}
            title="New chat"
          >
            <PlusIcon />
          </button>
          <span>{isOpen ? '\u25B2' : '\u25BC'}</span>
        </div>
      </div>

      {showAgentPicker && (
        <AgentPickerDropdown
          definitions={agentDefinitions}
          globalDefs={agentGlobalDefs}
          projectDefs={agentProjectDefs}
          showScopeToggle={agentShowScopeToggle}
          hasGlobal={agentHasGlobal}
          hasProject={agentHasProject}
          onSelect={(agentName) => {
            onNewChat(agentName)
            setShowAgentPicker(false)
            setIsOpen(false)
          }}
          onClose={() => setShowAgentPicker(false)}
        />
      )}

      {isOpen && (
        <div className="mobile-chat-drawer-content">
          <div className="mobile-chat-drawer-list">
            {sessions.length === 0 && (
              <div className="mobile-chat-drawer-empty">
                No conversations
              </div>
            )}
            {sessions.map((session) => {
              const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null;
              const titleText = session.title || `Chat ${session.ref}`;
              const displayTitle = seqLabel ? `${seqLabel}: ${titleText}` : titleText;
              const isActive = session.external_id === activeSessionId
              return (
                <div
                  key={session.id}
                  className={`session-item ${isActive ? 'attached' : ''}`}
                  onClick={() => { onSelectSession(session); setIsOpen(false) }}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectSession(session); setIsOpen(false) } }}
                >
                  <div className="session-item-main">
                    <span className={`session-source-dot ${session.status === 'paused' ? 'status-paused' : 'web-chat'}`} />
                    <span className="session-name" title={displayTitle}>
                      {displayTitle}
                    </span>
                  </div>
                  <div className="session-item-actions">
                    <span className="session-pid">
                      {formatRelativeTime(session.updated_at)}
                    </span>
                    {onDeleteSession && (
                      <button
                        type="button"
                        className="session-delete-btn"
                        title="Delete chat"
                        onClick={(e) => { e.stopPropagation(); onDeleteSession(session) }}
                      >
                        <TrashIcon />
                      </button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function ChatsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

function TrashIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  )
}
