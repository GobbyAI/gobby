import type { SessionInteractionMode, SessionObservationMeta } from '../../types/chat'

interface AgentStatusBarProps {
  viewingMeta: SessionObservationMeta
  interactionMode: SessionInteractionMode
  isAttached?: boolean
  isAutonomousSession?: boolean
  onAttach?: () => void
  onDetach?: () => void
}

const SOURCE_CONFIG: Record<string, { label: string; badgeClassName: string }> = {
  claude: { label: 'Claude', badgeClassName: 'chat-session-status__source--claude' },
  gemini: { label: 'Gemini', badgeClassName: 'chat-session-status__source--gemini' },
  qwen: { label: 'Qwen', badgeClassName: 'chat-session-status__source--qwen' },
  codex: { label: 'Codex', badgeClassName: 'chat-session-status__source--codex' },
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
  const sourceBadgeClassName =
    sourceConfig?.badgeClassName ?? 'chat-session-status__source--default'
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
          <span className={`chat-session-status__source ${sourceBadgeClassName}`}>
            {sourceLabel}
          </span>
          {viewingMeta.model && (
            <span className="chat-session-status__model">{viewingMeta.model}</span>
          )}
          <span className="chat-session-status__kind">{sessionTypeLabel}</span>
          {modeLabel && (
            <span className="chat-session-status__mode">
              Mode: {modeLabel}
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
            className="session-pane-action"
            onClick={onDetach}
          >
            Back
          </button>
        )}
        {!isAttached && !isAutonomousSession && onAttach && (
          <button
            type="button"
            className="session-pane-action session-pane-action--primary"
            onClick={onAttach}
          >
            Resume
          </button>
        )}
        {isAttached && onDetach && (
          <button
            type="button"
            className="session-pane-action"
            onClick={onDetach}
          >
            Detach
          </button>
        )}
      </div>
    </div>
  )
}
