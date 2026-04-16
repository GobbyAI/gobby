import type { SessionInteractionMode, SessionObservationMeta } from '../../types/chat'

interface AgentStatusBarProps {
  viewingMeta: SessionObservationMeta
  interactionMode: SessionInteractionMode
  isAttached?: boolean
  isAutonomousSession?: boolean
  onAttach?: () => void
  onResume?: () => void
  onDetach?: () => void
}

function formatSessionType(sessionType: SessionObservationMeta['sessionType']): string {
  return sessionType === 'web_chat' ? 'Web Chat' : 'tmux'
}

export function AgentStatusBar({
  viewingMeta,
  interactionMode: _interactionMode,
  isAttached = false,
  isAutonomousSession = false,
  onAttach,
  onResume,
  onDetach,
}: AgentStatusBarProps) {
  const sessionTypeLabel = formatSessionType(viewingMeta.sessionType)

  return (
    <div className="agent-status-bar" data-testid="agent-status-bar">
      <div className="agent-status-bar__summary">
        <div className="chat-session-status">
          {viewingMeta.model && (
            <span className="chat-session-status__model">{viewingMeta.model}</span>
          )}
          <span className="chat-session-status__kind">{sessionTypeLabel}</span>
          {(viewingMeta.agentName || viewingMeta.workflowName) && (
            <span className="chat-session-status__agent">
              {viewingMeta.agentName ?? viewingMeta.workflowName}
            </span>
          )}
        </div>
      </div>
      <div className="agent-status-bar__actions">
        {!isAttached && !isAutonomousSession && onAttach && (
          <button
            type="button"
            className="session-pane-action"
            onClick={onAttach}
          >
            Attach
          </button>
        )}
        {!isAttached && !isAutonomousSession && onResume && (
          <button
            type="button"
            className="session-pane-action"
            onClick={onResume}
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
