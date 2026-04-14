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
  const status =
    interactionMode === 'proxy' || isAttached ? 'Attached' : 'Observing'
  const actionButton = (() => {
    if (isAutonomousSession && !isAttached) return null
    if (isAttached && onDetach) {
      return (
        <button
          type="button"
          className="chat-session-status__action"
          onClick={onDetach}
        >
          Detach
        </button>
      )
    }
    if (!isAttached && onAttach) {
      return (
        <button
          type="button"
          className="chat-session-status__action chat-session-status__action--attach"
          onClick={onAttach}
        >
          Attach
        </button>
      )
    }
    return null
  })()

  return (
    <div className="agent-status-bar">
      {(viewingMeta.agentName || viewingMeta.workflowName) && (
        <div className="agent-status-bar__identity">
          <span aria-hidden="true">{"\uD83E\uDD16"}</span>
          <span className="agent-status-bar__name">
            {viewingMeta.agentName ?? 'Agent'}
          </span>
          {viewingMeta.workflowName && (
            <span className="agent-status-bar__workflow">
              {viewingMeta.workflowName}
            </span>
          )}
        </div>
      )}
      <div className="chat-session-status">
        <span
          className={`chat-session-status__dot ${sourceDotClassName}`}
          aria-hidden="true"
        />
        <span className="chat-session-status__label">{sourceLabel}</span>
        {modeLabel && (
          <span className="chat-session-status__mode">
            {modeLabel}
          </span>
        )}
        <span className="chat-session-status__status">
          {status}
          {status === 'Observing' && isLive ? ' (live)' : ''}
        </span>
      </div>
      {actionButton}
    </div>
  )
}
