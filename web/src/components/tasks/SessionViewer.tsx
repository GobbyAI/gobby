import { useState, useEffect, useCallback } from 'react'
import { SOURCE_LABELS } from '../shared/sourceTheme'

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

const ROOT_CLS = 'flex flex-col gap-2'
const STATE_CLS = 'py-2 text-[length:var(--text-sm)] text-[var(--text-muted)]'
const CARD_CLS = 'rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-3 py-[10px]'
const HEADER_CLS = 'flex flex-wrap items-center gap-2'
const DOT_CLS = 'h-[7px] w-[7px] shrink-0 rounded-full'
const REF_CLS = 'font-[inherit] text-[length:var(--text-sm)] font-semibold text-[var(--accent)]'
const SOURCE_CLS =
  'rounded bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const META_CLS = 'font-[inherit] text-[length:var(--text-xs)] text-[var(--text-muted)]'
const TITLE_CLS = 'mt-1 text-[length:var(--text-sm)] leading-[1.3] text-[var(--text-secondary)]'
const MODEL_CLS = 'mt-0.5 font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'
const TOGGLE_CLS =
  'cursor-pointer border-none bg-transparent p-0 text-left text-[length:var(--text-sm)] text-[var(--accent)] hover:underline'
const TRANSCRIPT_CLS =
  'flex max-h-60 flex-col gap-1 overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--bg-primary)] p-2'
const MSG_CLS = 'flex gap-2 py-[3px] text-[length:var(--text-sm)] leading-[1.4]'
const MSG_ROLE_COLOR: Record<string, string> = {
  assistant: 'text-[var(--text-primary)]',
  user: 'text-[var(--accent)]',
  tool: 'text-[var(--text-muted)]',
}
const MSG_ROLE_CLS =
  'min-w-[60px] shrink-0 font-[inherit] text-[length:var(--text-2xs)] font-semibold uppercase text-[var(--text-muted)]'
const MSG_CONTENT_CLS = 'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap'
const MORE_CLS = 'py-1 text-center text-[length:var(--text-xs)] text-[var(--text-muted)]'

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

const STATUS_COLORS: Record<string, string> = {
  active: 'var(--color-success-foreground)',
  idle: 'var(--color-warning-foreground)',
  closed: 'var(--text-muted)',
  error: 'var(--color-error)',
}

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

  if (isLoading) return <div className={STATE_CLS}>Loading session...</div>
  if (!session) return <div className={STATE_CLS}>Session not found</div>

  const statusColor = STATUS_COLORS[session.status] || 'var(--text-muted)'
  const sourceLabel = SOURCE_LABELS[session.source] || session.source
  const dur = duration(session.created_at, session.updated_at)
  const previewMessages = messages.filter(m => m.content || m.tool_name)
  const visibleMessages = previewMessages.slice(0, 8)
  const hiddenMessageCount = previewMessages.length - visibleMessages.length

  return (
    <div className={ROOT_CLS}>
      <div className={CARD_CLS}>
        <div className={HEADER_CLS}>
          <span className={DOT_CLS} style={{ background: statusColor }} />
          <span className={REF_CLS}>{session.ref}</span>
          <span className={SOURCE_CLS}>{sourceLabel}</span>
          <span className={META_CLS}>{session.message_count} msgs</span>
          <span className={META_CLS}>{dur}</span>
          <span className={META_CLS}>{relativeTime(session.updated_at)} ago</span>
        </div>
        {session.title && <div className={TITLE_CLS}>{session.title}</div>}
        {session.model && <div className={MODEL_CLS}>{session.model}</div>}
      </div>

      <button
        className={TOGGLE_CLS}
        onClick={() => setShowTranscript(!showTranscript)}
      >
        {showTranscript ? 'Hide transcript' : 'Show transcript preview'}
      </button>

      {showTranscript && previewMessages.length > 0 && (
        <div className={TRANSCRIPT_CLS}>
          {visibleMessages.map((m, i) => (
            <div
              key={`${m.timestamp}-${i}`}
              className={`${MSG_CLS} ${MSG_ROLE_COLOR[m.role] || ''}`}
            >
              <span className={MSG_ROLE_CLS}>
                {m.tool_name ? m.tool_name : m.role}
              </span>
              <span className={MSG_CONTENT_CLS}>
                {(m.content || '').slice(0, 200)}
                {(m.content || '').length > 200 ? '...' : ''}
              </span>
            </div>
          ))}
          {hiddenMessageCount > 0 && (
            <div className={MORE_CLS}>
              + {hiddenMessageCount} more messages
            </div>
          )}
        </div>
      )}
    </div>
  )
}
