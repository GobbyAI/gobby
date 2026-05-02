import { useState, useEffect, useRef } from 'react'
import type { GobbySession } from '../../types/sessions'
import type { SessionMessage } from '../../hooks/useSessionDetail'
import { useSessionTokenEvents } from '../../hooks/useSessionTokenEvents'
import { SourceIcon } from '../shared/SourceIcon'
import { BranchIcon, ChatIcon, SummaryIcon } from '../shared/Icons'
import { MemoizedMarkdown } from '../shared/MemoizedMarkdown'
import { SessionTranscript } from './SessionTranscript'
import { SessionLineage } from './SessionLineage'
import { SessionTokenTimeline } from './SessionTokenTimeline'
import { SessionModelBreakdown } from './SessionModelBreakdown'
import { ConfirmDialog } from '../chat/ui/ConfirmDialog'
import { DURATION_INVALID, formatDuration, formatTokens } from '../../utils/formatTime'
import { getSessionTitleText } from '../../lib/sessionTitle'
import { cn } from '../../lib/utils'
import { STATUS_BADGE_CLS, STATUS_BADGE_BG } from './styles'

const DETAIL_CLS = 'relative flex-1 overflow-y-auto'

const STICKY_HEADER_CLS =
  'sticky top-0 z-10 border-b border-[var(--border)] bg-[var(--bg-primary)] px-6 py-3'
const HEADER_CLS = 'flex flex-wrap items-center justify-between gap-2'
const HEADER_LEFT_CLS = 'flex min-w-0 items-center gap-2'
const HEADER_RIGHT_CLS = 'flex shrink-0 items-center gap-2'
const TITLE_CLS =
  'overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-xl)] font-semibold text-[var(--text-primary)]'
const TITLE_INPUT_CLS =
  'w-full min-w-0 rounded-sm border border-[var(--accent)] bg-[var(--bg-primary)] px-1 py-0 font-[inherit] text-[length:var(--text-xl)] font-semibold text-[var(--text-primary)] outline-none'

const MODEL_TAG_CLS =
  'rounded bg-[var(--bg-tertiary)] px-1.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)]'
const BRANCH_TAG_CLS =
  'flex items-center gap-1 rounded bg-[var(--bg-tertiary)] px-1.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)]'

const COMPACT_STATS_CLS = 'mt-1.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)]'

const METADATA_PANEL_CLS = 'px-6 py-4'
const METADATA_TOGGLE_CLS =
  'flex cursor-pointer select-none list-none items-center gap-1.5 py-1.5 text-[length:var(--text-base)] font-semibold uppercase tracking-[0.03em] text-[var(--text-secondary)] [&::-webkit-details-marker]:hidden'
const METADATA_CHEVRON_CLS =
  'shrink-0 -rotate-90 transition-transform duration-150 group-open:rotate-0'
const METADATA_CONTENT_CLS = 'py-2'

const GENERATE_BTN_CLS =
  'ml-auto flex cursor-pointer items-center gap-1.5 rounded border-0 bg-[var(--accent)] px-2.5 py-1 text-[length:var(--text-sm)] text-white transition-colors duration-150 hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11'
const REGENERATE_BTN_CLS =
  'ml-auto flex cursor-pointer items-center rounded border border-[var(--border)] bg-transparent p-1 text-[var(--text-muted)] transition-all duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'

const GENERATING_CLS =
  'flex items-center gap-2 py-3 text-[length:var(--text-base)] text-[var(--text-secondary)]'
const NO_SUMMARY_CLS = 'italic text-[length:var(--text-base)] text-[var(--text-muted)]'

const ACTIONS_CLS = 'relative'
const ASK_BTN_CLS =
  'flex cursor-pointer items-center gap-1 rounded-md border-0 bg-[var(--accent)] px-2.5 py-1 text-[length:var(--text-sm)] font-medium text-white transition-colors duration-150 hover:bg-[var(--accent-hover)] pointer-coarse:min-h-11'
const DROPDOWN_CLS =
  'absolute right-0 top-full z-20 mt-1 min-w-[200px] rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]'
const DROPDOWN_ITEM_CLS =
  'flex w-full cursor-pointer items-center gap-2 rounded-md border-0 bg-transparent px-3 py-2 text-left text-[length:var(--text-md)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent pointer-coarse:min-h-11'

interface SessionDetailProps {
  session: GobbySession
  messages: SessionMessage[]
  totalMessages: number
  isLoading: boolean
  onAskGobby?: (context: string) => void
  onContinueInChat?: (session: GobbySession) => void
  onWatchInChat?: (session: GobbySession) => void
  onRenameSession?: (id: string, title: string) => void
  onGenerateSummary: () => void
  isGeneratingSummary: boolean
  allSessions: GobbySession[]
  onSelectSession: (sessionId: string) => void
}

function statusLabel(status: string): string {
  switch (status) {
    case 'active': return 'Active'
    case 'archived': return 'Archived'
    case 'handoff_ready': return 'Handoff'
    case 'expired': return 'Expired'
    default: return status
  }
}

function formatCompactStats(session: GobbySession): string {
  const parts: string[] = []
  if (session.message_count != null) parts.push(`${session.message_count} msgs`)
  if (session.usage_input_tokens > 0) parts.push(`${formatTokens(session.usage_input_tokens)} in`)
  if (session.usage_output_tokens > 0) parts.push(`${formatTokens(session.usage_output_tokens)} out`)
  const dur = formatDuration(session.created_at, session.updated_at)
  if (dur !== DURATION_INVALID) parts.push(dur)
  if ((session.commit_count ?? 0) > 0) parts.push(`${session.commit_count} commits`)
  if ((session.tasks_closed ?? 0) > 0) parts.push(`${session.tasks_closed} tasks`)
  if ((session.memories_created ?? 0) > 0) parts.push(`${session.memories_created} memories`)
  if (session.had_edits) parts.push('edited files')
  return parts.join(' · ')
}

export function SessionDetail({
  session,
  messages,
  totalMessages,
  isLoading,
  onAskGobby,
  onContinueInChat,
  onWatchInChat,
  onRenameSession,
  onGenerateSummary,
  isGeneratingSummary,
  allSessions,
  onSelectSession,
}: SessionDetailProps) {
  const title = getSessionTitleText(session.title)
  const tokenEventsEnabled =
    import.meta.env.VITE_TOKEN_EVENTS !== '0' &&
    import.meta.env.VITE_TOKEN_EVENTS !== 'false'
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editValue, setEditValue] = useState('')
  const saveOnBlurRef = useRef(true)
  const { events: tokenEvents, breakdown: tokenBreakdown } = useSessionTokenEvents(
    tokenEventsEnabled ? session.id : null,
  )

  useEffect(() => {
    setIsEditingTitle(false)
  }, [session.id])

  return (
    <div className={DETAIL_CLS}>
      <div className={STICKY_HEADER_CLS}>
        <div className={HEADER_CLS}>
          <div className={HEADER_LEFT_CLS}>
            <SourceIcon source={session.source} size={18} />
            {isEditingTitle ? (
              <input
                className={TITLE_INPUT_CLS}
                value={editValue}
                onChange={e => setEditValue(e.target.value)}
                onBlur={() => {
                  if (saveOnBlurRef.current && onRenameSession) {
                    onRenameSession(session.id, editValue)
                  }
                  saveOnBlurRef.current = true
                  setIsEditingTitle(false)
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    saveOnBlurRef.current = false
                    if (onRenameSession) onRenameSession(session.id, editValue)
                    setIsEditingTitle(false)
                  } else if (e.key === 'Escape') {
                    saveOnBlurRef.current = false
                    setIsEditingTitle(false)
                  }
                }}
                aria-label="Rename session"
                autoFocus
              />
            ) : (
              <h2
                className={TITLE_CLS}
                onDoubleClick={() => {
                  if (!onRenameSession) return
                  setIsEditingTitle(true)
                  setEditValue(title)
                }}
              >
                {title}
              </h2>
            )}
          </div>
          <div className={HEADER_RIGHT_CLS}>
            <span className={cn(STATUS_BADGE_CLS, STATUS_BADGE_BG[session.status] ?? '')}>
              {statusLabel(session.status)}
            </span>
            {session.model && (
              <span className={MODEL_TAG_CLS}>{session.model}</span>
            )}
            {session.git_branch && (
              <span className={BRANCH_TAG_CLS}>
                <BranchIcon /> {session.git_branch}
              </span>
            )}
            {(onAskGobby || onContinueInChat || onWatchInChat) && (
              <SessionActions
                session={session}
                title={title}
                onAskGobby={onAskGobby}
                onContinueInChat={onContinueInChat}
                onWatchInChat={onWatchInChat}
              />
            )}
          </div>
        </div>
        <div className={COMPACT_STATS_CLS}>
          {formatCompactStats(session)}
        </div>
        {tokenEventsEnabled && (
          <div className="mt-3 grid gap-3 md:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
            <SessionTokenTimeline events={tokenEvents} />
            <SessionModelBreakdown breakdown={tokenBreakdown} />
          </div>
        )}
      </div>

      <div className={METADATA_PANEL_CLS}>
        <details open className="group mb-3">
          <summary className={METADATA_TOGGLE_CLS}>
            <ChevronIcon /> Summary
            {!session.summary_markdown && !isGeneratingSummary && (
              <button
                className={GENERATE_BTN_CLS}
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onGenerateSummary() }}
              >
                <SummaryIcon /> Generate
              </button>
            )}
            {session.summary_markdown && !isGeneratingSummary && (
              <button
                className={REGENERATE_BTN_CLS}
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onGenerateSummary() }}
                title="Regenerate summary"
                aria-label="Regenerate summary"
              >
                <SummaryIcon />
              </button>
            )}
          </summary>
          <div className={METADATA_CONTENT_CLS}>
            {isGeneratingSummary && (
              <div className={GENERATING_CLS}>
                <span className="thinking-spinner" /> Generating summary...
              </div>
            )}
            {session.summary_markdown && (
              <div className="message-content">
                <MemoizedMarkdown content={session.summary_markdown} id={`summary-${session.id}`} />
              </div>
            )}
            {!session.summary_markdown && !isGeneratingSummary && (
              <div className={NO_SUMMARY_CLS}>No summary available yet.</div>
            )}
          </div>
        </details>

        <SessionLineage
          session={session}
          allSessions={allSessions}
          onSelectSession={onSelectSession}
        />
      </div>

      <SessionTranscript
        messages={messages}
        totalMessages={totalMessages}
        isLoading={isLoading}
      />
    </div>
  )
}

function SessionActions({
  session,
  title,
  onAskGobby,
  onContinueInChat,
  onWatchInChat,
}: {
  session: GobbySession
  title: string
  onAskGobby?: (context: string) => void
  onContinueInChat?: (session: GobbySession) => void
  onWatchInChat?: (session: GobbySession) => void
}) {
  const [dropdownOpen, setDropdownOpen] = useState(false)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!dropdownOpen) return
    const handleClickOutside = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [dropdownOpen])

  const hasMessages = (session.message_count ?? 0) > 0
  const isActiveTerminal = session.status === 'active' && session.session_type === 'terminal'

  return (
    <div className={ACTIONS_CLS} ref={dropdownRef}>
      <button
        className={ASK_BTN_CLS}
        onClick={() => setDropdownOpen(!dropdownOpen)}
      >
        <ChatIcon /> Ask Gobby
        <ChevronDownIcon />
      </button>
      {dropdownOpen && (
        <div className={DROPDOWN_CLS}>
          {onWatchInChat && session.session_type === 'terminal' && (
            <button
              className={DROPDOWN_ITEM_CLS}
              disabled={!hasMessages}
              title={!hasMessages ? 'No messages recorded' : 'Watch this CLI session live in chat'}
              onClick={() => {
                setDropdownOpen(false)
                onWatchInChat(session)
              }}
            >
              <WatchIcon /> Watch in Chat
            </button>
          )}
          {onContinueInChat && (
            <button
              className={DROPDOWN_ITEM_CLS}
              disabled={!hasMessages}
              title={!hasMessages ? 'No messages recorded' : isActiveTerminal
                ? 'Take over this terminal session in web chat (terminal will be closed)'
                : 'Continue this session in chat with full history'}
              onClick={() => {
                setDropdownOpen(false)
                if (isActiveTerminal) {
                  setConfirmOpen(true)
                } else {
                  onContinueInChat(session)
                }
              }}
            >
              <ResumeIcon /> {isActiveTerminal ? 'Continue in Chat' : 'Resume Session'}
            </button>
          )}
          {onAskGobby && (
            <button
              className={DROPDOWN_ITEM_CLS}
              onClick={() => {
                setDropdownOpen(false)
                onAskGobby(`Tell me about session ${session.ref || 'unknown'} (${title}). Here's the summary:\n\n${session.summary_markdown || 'No summary available.'}`)
              }}
            >
              <ChatIcon /> New Chat with Summary
            </button>
          )}
        </div>
      )}
      {onContinueInChat && (
        <ConfirmDialog
          open={confirmOpen}
          title="Continue in Chat"
          description="This will end the terminal session and resume it here in the web chat. The terminal pane will be closed."
          confirmLabel="Continue"
          cancelLabel="Cancel"
          onConfirm={() => {
            setConfirmOpen(false)
            onContinueInChat(session)
          }}
          onCancel={() => setConfirmOpen(false)}
        />
      )}
    </div>
  )
}

function ChevronDownIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

function ChevronIcon() {
  return (
    <svg className={METADATA_CHEVRON_CLS} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

function WatchIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  )
}

function ResumeIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="5 3 19 12 5 21 5 3" />
    </svg>
  )
}
