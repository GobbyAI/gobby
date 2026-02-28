import { useState } from 'react'
import type { TmuxSession } from '../../hooks/useTmuxSessions'

interface MobileTerminalDrawerProps {
  sessions: TmuxSession[]
  attachedSession: string | null
  streamingId: string | null
  terminalNames: Record<string, string>
  isInteractive: boolean
  onAttach: (name: string, socket: string) => void
  onCreate: () => void
  onSetInteractive: (interactive: boolean) => void
  onKill: (name: string, socket: string) => void
}

export function MobileTerminalDrawer({
  sessions,
  attachedSession,
  streamingId,
  terminalNames,
  isInteractive,
  onAttach,
  onCreate,
  onSetInteractive,
  onKill,
}: MobileTerminalDrawerProps) {
  const [isOpen, setIsOpen] = useState(false)

  const attachedEntry = attachedSession
    ? sessions.find(s => s.name === attachedSession && streamingId !== null)
    : null
  const activeTitle = attachedEntry
    ? (terminalNames[`${attachedEntry.socket}:${attachedEntry.name}`]
      || attachedEntry.session_title || attachedEntry.pane_title || attachedEntry.window_name || attachedEntry.name)
    : 'Terminals'

  const defaultSessions = sessions.filter(s => s.socket === 'default')
  const gobbySessions = sessions.filter(s => s.socket === 'gobby')

  return (
    <div className={`mobile-chat-drawer ${isOpen ? '' : 'collapsed'}`}>
      <div
        className="mobile-chat-drawer-header"
        onClick={() => setIsOpen(!isOpen)}
      >
        <span className="mobile-chat-drawer-title">
          <TerminalIcon />
          {isOpen ? 'Terminals' : activeTitle}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          {isOpen ? (
              <button
                type="button"
                className="mobile-drawer-action"
                onClick={(e) => { e.stopPropagation(); onCreate(); setIsOpen(false) }}
                title="New terminal"
              >
                <PlusIcon />
              </button>
          ) : streamingId && (
            <>
              <span className={`mode-badge ${isInteractive ? 'mode-edit' : 'mode-view'}`}>
                {isInteractive ? 'EDIT' : 'VIEW'}
              </span>
              <button
                type="button"
                className={isInteractive ? 'mobile-drawer-detach-btn' : 'mobile-drawer-attach-btn'}
                onClick={(e) => { e.stopPropagation(); onSetInteractive(!isInteractive) }}
              >
                {isInteractive ? 'Detach' : 'Attach'}
              </button>
              <button
                type="button"
                className="mobile-drawer-kill-btn"
                onClick={(e) => { e.stopPropagation(); onKill(attachedEntry!.name, attachedEntry!.socket) }}
                title="Close terminal"
              >
                <TrashIcon />
              </button>
            </>
          )}
          <span>{isOpen ? '\u25B2' : '\u25BC'}</span>
        </div>
      </div>

      {isOpen && (
        <div className="mobile-chat-drawer-content">
          {sessions.length === 0 && (
            <div className="mobile-chat-drawer-empty">
              No terminals found
            </div>
          )}

          {defaultSessions.length > 0 && (
            <DrawerGroup
              sessions={defaultSessions}
              attachedSession={attachedSession}
              streamingId={streamingId}
              terminalNames={terminalNames}
              onAttach={(name, socket) => { onAttach(name, socket); setIsOpen(false) }}
            />
          )}

          {gobbySessions.length > 0 && (
            <DrawerGroup
              label="Agent Terminals"
              sessions={gobbySessions}
              attachedSession={attachedSession}
              streamingId={streamingId}
              terminalNames={terminalNames}
              onAttach={(name, socket) => { onAttach(name, socket); setIsOpen(false) }}
            />
          )}
        </div>
      )}
    </div>
  )
}

function DrawerGroup({
  label,
  sessions,
  attachedSession,
  streamingId,
  terminalNames,
  onAttach,
}: {
  label?: string
  sessions: TmuxSession[]
  attachedSession: string | null
  streamingId: string | null
  terminalNames: Record<string, string>
  onAttach: (name: string, socket: string) => void
}) {
  return (
    <div className="mobile-chat-drawer-list">
      {label && <div className="session-group-label" style={{ padding: '0.375rem 1rem' }}>{label}</div>}
      {sessions.map((session) => {
        const isAttached = attachedSession === session.name && streamingId !== null
        const nameKey = `${session.socket}:${session.name}`
        const displayName = terminalNames[nameKey] || session.session_title || session.pane_title || session.window_name || session.name

        return (
          <div
            key={`${session.socket}-${session.name}`}
            className={`session-item ${isAttached ? 'attached' : ''}`}
            onClick={() => onAttach(session.name, session.socket)}
            role="button"
            tabIndex={0}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onAttach(session.name, session.socket) } }}
          >
            <div className="session-item-main">
              <span className={`session-dot ${session.socket === 'gobby' ? 'agent' : 'user'}`} />
              <span className="session-name">{displayName}</span>
              {session.agent_managed && (
                <span className="session-badge agent-badge">agent</span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

function TerminalIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
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
