import { useState, useMemo, useRef } from 'react'
import type { GobbySession, SessionFilters } from '../../types/sessions'
import { KNOWN_SOURCES } from '../../types/sessions'
import { useNow } from '../../hooks/useNow'
import { useSessionDetail } from '../../hooks/useSessionDetail'
import { SessionDetail } from './SessionDetail'
import { SourceIcon } from '../shared/SourceIcon'
import { SOURCE_LABELS } from '../shared/sourceTheme'
import { formatRelativeTime } from '../../utils/formatTime'
import { MobileSessionDrawer } from './MobileSessionDrawer'
import { getSessionTitleText } from '../../lib/sessionTitle'
import { cn } from '../../lib/utils'
import { MODEL_BADGE_CLS, META_COUNT_CLS } from './styles'

const PAGE_CLS = 'flex flex-1 overflow-hidden'

const BROWSER_CLS =
  'flex w-[var(--sidebar-width)] min-w-[var(--sidebar-width)] flex-col overflow-hidden border-r border-[var(--border)] bg-[var(--bg-secondary)] transition-[width,min-width] duration-200 max-md:hidden'
const BROWSER_COLLAPSED_CLS = 'w-10 min-w-10'

const MAIN_CLS = 'flex flex-1 flex-col overflow-hidden bg-[var(--bg-primary)]'
const EMPTY_CLS =
  'flex flex-1 flex-col items-center justify-center gap-3 text-center text-[var(--text-muted)] [&_svg]:opacity-30'
const EMPTY_TITLE_CLS = 'text-[length:var(--text-lg)] font-medium text-[var(--text-secondary)]'
const EMPTY_TEXT_CLS = 'text-[length:var(--text-base)]'

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
  return SOURCE_LABELS[source] ?? source
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
  const now = useNow()

  const detail = useSessionDetail(selectedSessionId)

  const grouped = useMemo(() => {
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
  }, [sessions, now])

  return (
    <div className={PAGE_CLS}>
      <div className={cn(BROWSER_CLS, !sidebarOpen && BROWSER_COLLAPSED_CLS)}>
        <div className="sessions-sidebar-header">
          {sidebarOpen && <span className="sessions-sidebar-title">Sessions</span>}
          <div className="sessions-sidebar-actions">
            <button
              className="terminals-sidebar-toggle"
              onClick={() => setSidebarOpen(!sidebarOpen)}
              title={sidebarOpen ? 'Collapse' : 'Expand'}
            >
              {sidebarOpen ? '◀' : '▶'}
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
                    const title = getSessionTitleText(session.title)
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
                            <span className={MODEL_BADGE_CLS}>
                              {session.model.split('-').slice(-1)[0]}
                            </span>
                          )}
                          <span className={META_COUNT_CLS}>{session.message_count}msg</span>
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

      <div className={MAIN_CLS}>
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
          <div className={EMPTY_CLS}>
            <SessionsIcon size={48} />
            <h3 className={EMPTY_TITLE_CLS}>Select a session</h3>
            <p className={EMPTY_TEXT_CLS}>Choose a session from the list to view details, stats, and transcript.</p>
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
