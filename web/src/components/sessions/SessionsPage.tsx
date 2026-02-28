import { useState, useMemo, useRef } from 'react'
import './SessionsPage.css'
import type { GobbySession, SessionFilters } from '../../hooks/useSessions'
import { KNOWN_SOURCES } from '../../hooks/useSessions'
import { useSessionDetail } from '../../hooks/useSessionDetail'
import { SessionDetail } from './SessionDetail'
import { SourceIcon } from '../shared/SourceIcon'
import { formatRelativeTime } from '../../utils/formatTime'
import { MobileSessionDrawer } from './MobileSessionDrawer'

interface SessionsPageProps {
  sessions: GobbySession[]
  filters: SessionFilters
  onFiltersChange: (filters: SessionFilters) => void
  isLoading: boolean
  onAskGobby?: (context: string) => void
  onContinueInChat?: (session: GobbySession) => void
  onWatchInChat?: (session: GobbySession) => void
  onRenameSession?: (id: string, title: string) => void
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'claude_sdk_web_chat': return 'Web Chat'
    case 'claude': return 'Claude'
    case 'gemini': return 'Gemini'
    case 'codex': return 'Codex'
    default: return source
  }
}

export function SessionsPage({
  sessions,
  filters,
  onFiltersChange,
  isLoading,
  onAskGobby,
  onContinueInChat,
  onWatchInChat,
  onRenameSession,
}: SessionsPageProps) {
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')
  const saveOnBlurRef = useRef(true)

  const detail = useSessionDetail(selectedSessionId)

  // Time-group sessions
  const grouped = useMemo(() => {
    const now = Date.now()
    const groups: { label: string; sessions: GobbySession[] }[] = [
      { label: 'Today', sessions: [] },
      { label: 'Yesterday', sessions: [] },
      { label: 'Previous 7 Days', sessions: [] },
      { label: 'Older', sessions: [] },
    ]

    for (const s of sessions) {
      const diffMs = now - new Date(s.updated_at).getTime()
      const diffDays = diffMs / 86_400_000
      if (diffDays < 1) groups[0].sessions.push(s)
      else if (diffDays < 2) groups[1].sessions.push(s)
      else if (diffDays < 7) groups[2].sessions.push(s)
      else groups[3].sessions.push(s)
    }

    return groups.filter((g) => g.sessions.length > 0)
  }, [sessions])

  return (
    <div className="sessions-page">
      {/* Left panel: session browser */}
      <div className={`sessions-browser ${sidebarOpen ? '' : 'collapsed'}`}>
        <div className="sessions-sidebar-header">
          {sidebarOpen && <span className="sessions-sidebar-title">Sessions</span>}
          <div className="sessions-sidebar-actions">
            <button
              className="terminals-sidebar-toggle"
              onClick={() => setSidebarOpen(!sidebarOpen)}
              title={sidebarOpen ? 'Collapse' : 'Expand'}
            >
              {sidebarOpen ? '\u25C0' : '\u25B6'}
            </button>
          </div>
        </div>

        {sidebarOpen && (
          <>
            <div className="sessions-filter-bar">
              <input
                className="sessions-filter-input"
                type="text"
                placeholder="Search sessions..."
                value={filters.search}
                onChange={(e) =>
                  onFiltersChange({ ...filters, search: e.target.value })
                }
              />
              <div className="sessions-filter-row">
                <select
                  className="sessions-filter-select"
                  value={filters.source || ''}
                  onChange={(e) =>
                    onFiltersChange({ ...filters, source: e.target.value || null })
                  }
                >
                  <option value="">All Sources</option>
                  {KNOWN_SOURCES.map((s) => (
                    <option key={s} value={s}>
                      {sourceLabel(s)}
                    </option>
                  ))}
                </select>
              </div>
              <select
                className="sessions-filter-select"
                value={filters.sortOrder}
                onChange={(e) =>
                  onFiltersChange({
                    ...filters,
                    sortOrder: e.target.value as 'newest' | 'oldest',
                  })
                }
              >
                <option value="newest">Newest First</option>
                <option value="oldest">Oldest First</option>
              </select>
            </div>

            <div className="sessions-list">
              {sessions.length === 0 && !isLoading && (
                <div className="terminals-empty-sidebar">No sessions found</div>
              )}
              {isLoading && sessions.length === 0 && (
                <div className="terminals-empty-sidebar">Loading...</div>
              )}

              {grouped.map((group) => (
                <div key={group.label} className="session-group">
                  <div className="session-group-label">{group.label}</div>
                  {group.sessions.map((session) => {
                    const title = session.title || `Untitled #${session.ref}`
                    const isSelected = session.id === selectedSessionId
                    const isEditing = editingId === session.id
                    return (
                      <div
                        key={session.id}
                        className={`session-item ${isSelected ? 'attached' : ''}`}
                        onClick={() => { if (!isEditing) setSelectedSessionId(session.id) }}
                      >
                        <div className="session-item-main">
                          <SourceIcon source={session.source} size={14} />
                          {isEditing ? (
                            <input
                              className="session-name-input"
                              value={editValue}
                              onChange={e => setEditValue(e.target.value)}
                              onBlur={() => {
                                if (saveOnBlurRef.current && onRenameSession) {
                                  onRenameSession(session.id, editValue)
                                }
                                saveOnBlurRef.current = true
                                setEditingId(null)
                              }}
                              onKeyDown={e => {
                                if (e.key === 'Enter') {
                                  saveOnBlurRef.current = false
                                  if (onRenameSession) onRenameSession(session.id, editValue)
                                  setEditingId(null)
                                } else if (e.key === 'Escape') {
                                  saveOnBlurRef.current = false
                                  setEditingId(null)
                                }
                              }}
                              onClick={e => e.stopPropagation()}
                              aria-label="Rename session"
                              autoFocus
                            />
                          ) : (
                            <span
                              className="session-name"
                              title={title}
                              onDoubleClick={e => {
                                if (!onRenameSession) return
                                e.stopPropagation()
                                setEditingId(session.id)
                                setEditValue(title)
                              }}
                            >
                              {title}
                            </span>
                          )}
                        </div>
                        <div className="session-item-actions">
                          {session.model && (
                            <span className="session-detail-model-badge">
                              {session.model.split('-').slice(-1)[0]}
                            </span>
                          )}
                          <span className="session-meta-count">{session.message_count}msg</span>
                          <span className="session-pid">
                            {formatRelativeTime(session.updated_at)}
                          </span>
                        </div>
                      </div>
                    )
                  })}
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Right panel: session detail or empty state */}
      <div className="sessions-main">
        <MobileSessionDrawer
          sessions={sessions}
          selectedSessionId={selectedSessionId}
          onSelectSession={setSelectedSessionId}
          isLoading={isLoading}
        />
        {detail.session ? (
          <SessionDetail
            session={detail.session}
            messages={detail.messages}
            totalMessages={detail.totalMessages}
            isLoading={detail.isLoading}
            onAskGobby={onAskGobby}
            onContinueInChat={onContinueInChat}
            onWatchInChat={onWatchInChat}
            onRenameSession={onRenameSession}
            onGenerateSummary={detail.generateSummary}
            isGeneratingSummary={detail.isGeneratingSummary}
            allSessions={sessions}
            onSelectSession={setSelectedSessionId}
          />
        ) : (
          <div className="sessions-empty">
            <SessionsIcon size={48} />
            <h3>Select a session</h3>
            <p>Choose a session from the list to view details, stats, and transcript.</p>
          </div>
        )}
      </div>
    </div>
  )
}

function SessionsIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  )
}
