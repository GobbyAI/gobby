import type { SessionInteractionMode, SessionObservationMeta } from '../../types/chat'

interface AgentStatusBarProps {
  viewingMeta: SessionObservationMeta
  interactionMode: SessionInteractionMode
  isAttached?: boolean
  isAutonomousSession?: boolean
  onAttach?: () => void
  onDetach?: () => void
}

const SOURCE_CONFIG: Record<string, { label: string; dotClassName: string }> = {
  claude: { label: 'Claude', dotClassName: 'chat-session-status__dot--claude' },
  gemini: { label: 'Gemini', dotClassName: 'chat-session-status__dot--gemini' },
  qwen: { label: 'Qwen', dotClassName: 'chat-session-status__dot--qwen' },
  codex: { label: 'Codex', dotClassName: 'chat-session-status__dot--codex' },
}

function formatChatMode(chatMode: string | null | undefined): string | null {
  switch (chatMode) {
    case 'plan':
      return 'Plan'
    case 'accept_edits':
      return 'Act'
    case 'bypass':
      return 'Auto'
    default:
      return null
  }
}

function formatSessionType(sessionType: SessionObservationMeta['sessionType']): string {
  return sessionType === 'web_chat' ? 'Web Chat' : 'tmux'
}

export function AgentStatusBar({
  viewingMeta,
  interactionMode,
  isAttached = false,
  isAutonomousSession = false,
  onAttach,
  onDetach,
}: AgentStatusBarProps) {
  const sourceConfig = SOURCE_CONFIG[viewingMeta.source]
  const sourceLabel = sourceConfig?.label ?? viewingMeta.source
  const sourceDotClassName =
    sourceConfig?.dotClassName ?? 'chat-session-status__dot--default'
  const modeLabel = formatChatMode(viewingMeta.chatMode)
  const isLive = viewingMeta.status === 'active'
  const statusLabel =
    interactionMode === 'proxy' || isAttached ? 'Attached' : 'Watching'
  const sessionTypeLabel = formatSessionType(viewingMeta.sessionType)
  const title = viewingMeta.title ?? 'Observed Session'

  return (
    <div className="agent-status-bar" data-testid="agent-status-bar">
      <div className="agent-status-bar__summary">
        <div className="agent-status-bar__session">
          {viewingMeta.ref && (
            <span className="agent-status-bar__ref">{viewingMeta.ref}</span>
          )}
          <span className="agent-status-bar__title">{title}</span>
          <span className="agent-status-bar__status">
            {statusLabel}
            {statusLabel === 'Watching' && isLive ? ' live' : ''}
          </span>
        </div>
        <div className="chat-session-status">
          <span
            className={`chat-session-status__dot ${sourceDotClassName}`}
            aria-hidden="true"
          />
          <span className="chat-session-status__label">{sourceLabel}</span>
          {viewingMeta.model && (
            <span className="chat-session-status__model">{viewingMeta.model}</span>
          )}
          <span className="chat-session-status__kind">{sessionTypeLabel}</span>
          {modeLabel && (
            <span className="chat-session-status__mode">
              {modeLabel}
            </span>
          )}
          {(viewingMeta.agentName || viewingMeta.workflowName) && (
            <span className="chat-session-status__agent">
              {viewingMeta.agentName ?? viewingMeta.workflowName}
            </span>
          )}
        </div>
      </div>
      <div className="agent-status-bar__actions">
        {!isAttached && onDetach && (
          <button
            type="button"
            className="chat-session-status__action"
            onClick={onDetach}
          >
            Back
          </button>
        )}
        {!isAttached && !isAutonomousSession && onAttach && (
          <button
            type="button"
            className="chat-session-status__action chat-session-status__action--attach"
            onClick={onAttach}
          >
            Resume
          </button>
        )}
        {isAttached && onDetach && (
          <button
            type="button"
            className="chat-session-status__action"
            onClick={onDetach}
          >
            Detach
          </button>
        )}
      </div>
    </div>
  )
}
