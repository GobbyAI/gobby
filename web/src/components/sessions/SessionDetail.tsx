import { useState, useEffect, useRef } from 'react'
import type { GobbySession } from '../../hooks/useSessions'
import type { SessionMessage } from '../../hooks/useSessionDetail'
import { SourceIcon } from '../shared/SourceIcon'
import { BranchIcon, ChatIcon, SummaryIcon } from '../shared/Icons'
import { MemoizedMarkdown } from '../shared/MemoizedMarkdown'
import { SessionTranscript } from './SessionTranscript'
import { SessionLineage } from './SessionLineage'
import { ConfirmDialog } from '../chat/ui/ConfirmDialog'
import { DURATION_INVALID, formatDuration, formatTokens, formatCost } from '../../utils/formatTime'

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
  if (session.usage_total_cost_usd > 0) parts.push(formatCost(session.usage_total_cost_usd))
  const dur = formatDuration(session.created_at, session.updated_at)
  if (dur !== DURATION_INVALID) parts.push(dur)
  if ((session.commit_count ?? 0) > 0) parts.push(`${session.commit_count} commits`)
  if ((session.tasks_closed ?? 0) > 0) parts.push(`${session.tasks_closed} tasks`)
  if ((session.memories_created ?? 0) > 0) parts.push(`${session.memories_created} memories`)
  if (session.had_edits) parts.push('edited files')
  return parts.join(' \u00b7 ')
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
  const title = session.title || `Session #${session.ref}`
  const [isEditingTitle, setIsEditingTitle] = useState(false)
  const [editValue, setEditValue] = useState('')
  const saveOnBlurRef = useRef(true)

  useEffect(() => {
    setIsEditingTitle(false)
  }, [session.id])

  return (
    <div className="session-detail">
      {/* Sticky header */}
      <div className="session-detail-sticky-header">
        <div className="session-detail-header">
          <div className="session-detail-header-left">
            <SourceIcon source={session.source} size={18} />
            {isEditingTitle ? (
              <input
                className="session-detail-title-input"
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
                className="session-detail-title"
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
          <div className="session-detail-header-right">
            <span className={`session-detail-status session-detail-status-${session.status}`}>
              {statusLabel(session.status)}
            </span>
            {session.model && (
              <span className="session-detail-model">{session.model}</span>
            )}
            {session.git_branch && (
              <span className="session-detail-branch">
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
        <div className="session-detail-compact-stats">
          {formatCompactStats(session)}
        </div>
      </div>

      {/* Collapsible metadata */}
      <div className="session-metadata-panel">
        {/* Summary */}
        <details open>
          <summary className="session-metadata-toggle">
            <ChevronIcon /> Summary
            {!session.summary_markdown && !isGeneratingSummary && (
              <button
                className="session-detail-generate-btn"
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onGenerateSummary() }}
              >
                <SummaryIcon /> Generate
              </button>
            )}
            {session.summary_markdown && !isGeneratingSummary && (
              <button
                className="session-detail-regenerate-btn"
                onClick={(e) => { e.preventDefault(); e.stopPropagation(); onGenerateSummary() }}
                title="Regenerate summary"
                aria-label="Regenerate summary"
              >
                <SummaryIcon />
              </button>
            )}
          </summary>
          <div className="session-metadata-content">
            {isGeneratingSummary && (
              <div className="session-detail-generating">
                <span className="thinking-spinner" /> Generating summary...
              </div>
            )}
            {session.summary_markdown && (
              <div className="message-content">
                <MemoizedMarkdown content={session.summary_markdown} id={`summary-${session.id}`} />
              </div>
            )}
            {!session.summary_markdown && !isGeneratingSummary && (
              <div className="session-detail-no-summary">No summary available yet.</div>
            )}
          </div>
        </details>

        {/* Lineage */}
        <SessionLineage
          session={session}
          allSessions={allSessions}
          onSelectSession={onSelectSession}
        />
      </div>

      {/* Transcript */}
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
  const isActiveTerminal = session.status === 'active' && session.source !== 'claude_sdk_web_chat'

  return (
    <div className="session-detail-actions" ref={dropdownRef}>
      <button
        className="session-detail-ask-btn"
        onClick={() => setDropdownOpen(!dropdownOpen)}
      >
        <ChatIcon /> Ask Gobby
        <ChevronDownIcon />
      </button>
      {dropdownOpen && (
        <div className="session-detail-dropdown">
          {onWatchInChat && session.source !== 'claude_sdk_web_chat' && (
            <button
              className="session-detail-dropdown-item"
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
              className="session-detail-dropdown-item"
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
              className="session-detail-dropdown-item"
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
    <svg className="session-metadata-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
