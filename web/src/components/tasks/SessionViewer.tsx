import { useState, useEffect, useCallback } from 'react'

// =============================================================================
// Types
// =============================================================================

interface SessionInfo {
  id: string
  ref: string
  source: string
  status: string
  title: string | null
  message_count: number
  created_at: string
  updated_at: string
  model: string | null
}

interface MessagePreview {
  role: string
  content: string | null
  tool_name: string | null
  timestamp: string
}

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

function duration(start: string, end: string): string {
  const ms = new Date(end).getTime() - new Date(start).getTime()
  const minutes = Math.floor(ms / 60000)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  const rem = minutes % 60
  return rem > 0 ? `${hours}h ${rem}m` : `${hours}h`
}

const SOURCE_LABELS: Record<string, string> = {
  claude: 'Claude Code',
  gemini: 'Gemini CLI',
  codex: 'Codex',
  'claude_sdk_web_chat': 'Web Chat',
}

const STATUS_COLORS: Record<string, string> = {
  active: '#22c55e',
  idle: '#f59e0b',
  closed: '#737373',
  error: '#ef4444',
}

// =============================================================================
// SessionViewer
// =============================================================================

interface SessionViewerProps {
  sessionId: string
}

export function SessionViewer({ sessionId }: SessionViewerProps) {
  const [session, setSession] = useState<SessionInfo | null>(null)
  const [messages, setMessages] = useState<MessagePreview[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [showTranscript, setShowTranscript] = useState(false)

  const fetchSession = useCallback(async () => {
    setIsLoading(true)
    try {
      const baseUrl = getBaseUrl()
      const [sessionRes, msgRes] = await Promise.all([
        fetch(`${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}`),
        fetch(`${baseUrl}/api/sessions/${encodeURIComponent(sessionId)}/messages?limit=10`),
      ])
      if (sessionRes.ok) {
        const data = await sessionRes.json()
        setSession(data.session || data)
      } else {
        console.warn(`Failed to fetch session ${sessionId}: ${sessionRes.status}`)
      }
      if (msgRes.ok) {
        const data = await msgRes.json()
        setMessages(data.messages || [])
      } else {
        console.warn(`Failed to fetch session messages: ${msgRes.status}`)
      }
    } catch (e) {
      console.error('Failed to fetch session:', e)
    }
    setIsLoading(false)
  }, [sessionId])

  useEffect(() => {
    fetchSession()
  }, [fetchSession])

  if (isLoading) return <div className="session-viewer-loading">Loading session...</div>
  if (!session) return <div className="session-viewer-empty">Session not found</div>

  const statusColor = STATUS_COLORS[session.status] || '#737373'
  const sourceLabel = SOURCE_LABELS[session.source] || session.source
  const dur = duration(session.created_at, session.updated_at)

  return (
    <div className="session-viewer">
      <div className="session-viewer-card">
        <div className="session-viewer-header">
          <span className="session-viewer-dot" style={{ background: statusColor }} />
          <span className="session-viewer-ref">{session.ref}</span>
          <span className="session-viewer-source">{sourceLabel}</span>
          <span className="session-viewer-meta">{session.message_count} msgs</span>
          <span className="session-viewer-meta">{dur}</span>
          <span className="session-viewer-meta">{relativeTime(session.updated_at)} ago</span>
        </div>
        {session.title && (
          <div className="session-viewer-title">{session.title}</div>
        )}
        {session.model && (
          <div className="session-viewer-model">{session.model}</div>
        )}
      </div>

      <button
        className="session-viewer-toggle"
        onClick={() => setShowTranscript(!showTranscript)}
      >
        {showTranscript ? 'Hide transcript' : 'Show transcript preview'}
      </button>

      {showTranscript && messages.length > 0 && (
        <div className="session-viewer-transcript">
          {messages
            .filter(m => m.content || m.tool_name)
            .slice(0, 8)
            .map((m, i) => (
              <div key={`${m.timestamp}-${i}`} className={`session-viewer-msg session-viewer-msg--${m.role}`}>
                <span className="session-viewer-msg-role">
                  {m.tool_name ? m.tool_name : m.role}
                </span>
                <span className="session-viewer-msg-content">
                  {(m.content || '').slice(0, 200)}
                  {(m.content || '').length > 200 ? '...' : ''}
                </span>
              </div>
            ))}
          {messages.length > 8 && (
            <div className="session-viewer-more">
              + {messages.length - 8} more messages
            </div>
          )}
        </div>
      )}
    </div>
  )
}
