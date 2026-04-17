import type { SessionInteractionMode, SessionObservationMeta } from '../../types/chat'

interface AgentStatusBarProps {
  viewingMeta: SessionObservationMeta
  interactionMode: SessionInteractionMode
  isAttached?: boolean
  isAutonomousSession?: boolean
  onAttach?: () => void
  onResume?: () => void
  onDetach?: () => void
  onTogglePanel?: () => void
  isPanelPinned?: boolean
}

function getSessionKindBadge(sessionType: SessionObservationMeta['sessionType']): {
  label: string
  className: string
} {
  if (sessionType === 'web_chat') {
    return { label: 'WEB', className: 'session-kind-badge--web' }
  }

  return { label: 'TMUX', className: 'session-kind-badge--tmux' }
}

function formatSessionStateText(
  interactionMode: SessionInteractionMode,
  isAttached: boolean,
): string {
  if (interactionMode === 'proxy' || isAttached) {
    return 'Attached'
  }

  return 'Watching live'
}

function PanelIcon({ pinned }: { pinned: boolean }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <line x1="15" y1="3" x2="15" y2="21" />
      {pinned && <line x1="18" y1="9" x2="21" y2="9" opacity="0.5" />}
    </svg>
  )
}

export function AgentStatusBar({
  viewingMeta,
  interactionMode,
  isAttached = false,
  isAutonomousSession = false,
  onAttach,
  onResume,
  onDetach,
  onTogglePanel,
  isPanelPinned = false,
}: AgentStatusBarProps) {
  const sessionBadge = getSessionKindBadge(viewingMeta.sessionType)
  const stateText = formatSessionStateText(interactionMode, isAttached)

  return (
    <div className="agent-status-bar" data-testid="agent-status-bar">
      <div className="agent-status-bar__summary">
        <div className="chat-session-status">
          <span className="chat-session-status__state">{stateText}</span>
          <span className={`session-kind-badge ${sessionBadge.className}`}>
            {sessionBadge.label}
          </span>
        </div>
      </div>
      <div className="agent-status-bar__actions">
        {onTogglePanel && (
          <button
            type="button"
            className="session-pane-action session-pane-action--icon"
            onClick={onTogglePanel}
            aria-label={isPanelPinned ? 'Hide activity panel' : 'Show activity panel'}
            title={isPanelPinned ? 'Hide activity panel' : 'Show activity panel'}
          >
            <PanelIcon pinned={isPanelPinned} />
          </button>
        )}
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
