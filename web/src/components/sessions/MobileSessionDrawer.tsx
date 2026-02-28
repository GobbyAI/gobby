import { useState } from 'react'
import type { GobbySession } from '../../hooks/useSessions'
import { SourceIcon } from '../shared/SourceIcon'
import { formatRelativeTime } from '../../utils/formatTime'

function getModelBadge(model: string): string {
  const parts = model.split('-')
  return parts[parts.length - 1]
}

interface MobileSessionDrawerProps {
  sessions: GobbySession[]
  selectedSessionId: string | null
  onSelectSession: (id: string) => void
  isLoading: boolean
}

export function MobileSessionDrawer({
  sessions,
  selectedSessionId,
  onSelectSession,
  isLoading,
}: MobileSessionDrawerProps) {
  const [isOpen, setIsOpen] = useState(false)

  const selectedSession = sessions.find(s => s.id === selectedSessionId)
  const activeTitle = selectedSession?.title || 'Sessions'

  return (
    <div className={`mobile-chat-drawer ${isOpen ? '' : 'collapsed'}`}>
      <div
        className="mobile-chat-drawer-header"
        role="button"
        tabIndex={0}
        aria-expanded={isOpen}
        onClick={() => setIsOpen(!isOpen)}
        onKeyDown={(e) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setIsOpen(!isOpen) } }}
      >
        <span className="mobile-chat-drawer-title">
          <SessionsIcon />
          {isOpen ? 'Sessions' : activeTitle}
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
          <span>{isOpen ? '\u25B2' : '\u25BC'}</span>
        </div>
      </div>

      {isOpen && (
        <div className="mobile-chat-drawer-content">
          {sessions.length === 0 && (
            <div style={{ padding: '0.75rem 1rem', color: 'var(--text-muted)', fontSize: 'calc(var(--font-size-base) * 0.85)' }}>
              {isLoading ? 'Loading...' : 'No sessions found'}
            </div>
          )}

          <div className="mobile-chat-drawer-list">
            {sessions.map((session) => {
              const title = session.title || `Untitled #${session.ref}`
              const isSelected = session.id === selectedSessionId

              return (
                <div
                  key={session.id}
                  className={`session-item ${isSelected ? 'attached' : ''}`}
                  onClick={() => { onSelectSession(session.id); setIsOpen(false) }}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectSession(session.id); setIsOpen(false) } }}
                >
                  <div className="session-item-main">
                    <SourceIcon source={session.source} size={14} />
                    <span className="session-name" title={title}>
                      {title}
                    </span>
                  </div>
                  <div className="session-item-actions">
                    {session.model && (
                      <span className="session-detail-model-badge">
                        {getModelBadge(session.model)}
                      </span>
                    )}
                    <span className="session-meta-count">{session.message_count} msg</span>
                    <span className="session-pid">
                      {formatRelativeTime(session.updated_at)}
                    </span>
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

function SessionsIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  )
}

