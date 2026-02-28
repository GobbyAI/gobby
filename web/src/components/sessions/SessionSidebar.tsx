import type { GobbySession, SessionFilters, ProjectInfo } from '../../hooks/useSessions'

interface SessionSidebarProps {
  sessions: GobbySession[]
  projects: ProjectInfo[]
  filters: SessionFilters
  onFiltersChange: (filters: SessionFilters) => void
  activeSessionId: string | null
  isLoading: boolean
  onNewChat: () => void
  onSelectSession: (session: GobbySession) => void
  isOpen: boolean
  onToggle: () => void
}

function formatRelativeTime(dateStr: string): string {
  const now = Date.now()
  const then = new Date(dateStr).getTime()
  const diffMs = now - then
  const diffMin = Math.floor(diffMs / 60000)
  if (diffMin < 1) return 'now'
  if (diffMin < 60) return `${diffMin}m ago`
  const diffHr = Math.floor(diffMin / 60)
  if (diffHr < 24) return `${diffHr}h ago`
  const diffDay = Math.floor(diffHr / 24)
  if (diffDay < 30) return `${diffDay}d ago`
  return new Date(dateStr).toLocaleDateString()
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'claude_sdk_web_chat': return 'Web'
    case 'claude': return 'Claude'
    case 'gemini': return 'Gemini'
    case 'codex': return 'Codex'
    default: return source
  }
}

function sourceDotClass(source: string): string {
  switch (source) {
    case 'claude_sdk_web_chat': return 'session-source-dot web-chat'
    case 'claude': return 'session-source-dot claude'
    case 'gemini': return 'session-source-dot gemini'
    case 'codex': return 'session-source-dot codex'
    default: return 'session-source-dot'
  }
}

export function SessionSidebar({
  sessions,
  projects,
  filters,
  onFiltersChange,
  activeSessionId,
  isLoading,
  onNewChat,
  onSelectSession,
  isOpen,
  onToggle,
}: SessionSidebarProps) {
  const webSessions = sessions.filter((s) => s.source === 'claude_sdk_web_chat')
  const cliSessions = sessions.filter((s) => s.source !== 'claude_sdk_web_chat')

  // Unique sources for the filter dropdown
  const sources = [...new Set(sessions.map((s) => s.source))].sort()

  return (
    <div className={`sessions-sidebar ${isOpen ? '' : 'collapsed'}`}>
      <div className="sessions-sidebar-header">
        {isOpen && <span className="sessions-sidebar-title">Sessions</span>}
        <div className="sessions-sidebar-actions">
          {isOpen && (
              <button
                className="terminals-action-btn"
                onClick={onNewChat}
                title="New Chat"
              >
                <PlusIcon />
              </button>
          )}
          <button
            className="terminals-sidebar-toggle"
            onClick={onToggle}
            title={isOpen ? 'Collapse sidebar' : 'Expand sidebar'}
          >
            {isOpen ? '\u25C0' : '\u25B6'}
          </button>
        </div>
      </div>

      {isOpen && (
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
                  onFiltersChange({
                    ...filters,
                    source: e.target.value || null,
                  })
                }
              >
                <option value="">All Sources</option>
                {sources.map((s) => (
                  <option key={s} value={s}>
                    {sourceLabel(s)}
                  </option>
                ))}
              </select>
              <select
                className="sessions-filter-select"
                value={filters.projectId || ''}
                onChange={(e) =>
                  onFiltersChange({
                    ...filters,
                    projectId: e.target.value || null,
                  })
                }
              >
                <option value="">All Projects</option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
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

            {webSessions.length > 0 && (
              <div className="session-group">
                <div className="session-group-label">Web Chat</div>
                {webSessions.map((session) => (
                  <SessionItem
                    key={session.id}
                    session={session}
                    isActive={session.external_id === activeSessionId}
                    onSelect={onSelectSession}
                  />
                ))}
              </div>
            )}

            {cliSessions.length > 0 && (
              <div className="session-group">
                <div className="session-group-label">CLI Sessions</div>
                {cliSessions.map((session) => (
                  <SessionItem
                    key={session.id}
                    session={session}
                    isActive={session.external_id === activeSessionId}
                    onSelect={onSelectSession}
                  />
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function SessionItem({
  session,
  isActive,
  onSelect,
}: {
  session: GobbySession
  isActive: boolean
  onSelect: (session: GobbySession) => void
}) {
  const isResumable = session.source === 'claude_sdk_web_chat' || session.source === 'claude'
  const title = session.title || `Untitled #${session.ref}`

  return (
    <div
      className={`session-item ${isActive ? 'attached' : ''} ${!isResumable ? 'session-item-muted' : ''}`}
      onClick={() => onSelect(session)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onSelect(session)
        }
      }}
    >
      <div className="session-item-main">
        <span className={sourceDotClass(session.source)} />
        <span className="session-name" title={title}>
          {title}
        </span>
      </div>
      <div className="session-item-actions">
        <span className="session-badge source-badge">{sourceLabel(session.source)}</span>
        <span className="session-pid">{formatRelativeTime(session.updated_at)}</span>
      </div>
    </div>
  )
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

