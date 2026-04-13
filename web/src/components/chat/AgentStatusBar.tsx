import type { SessionInteractionMode, SessionObservationMeta } from '../../types/chat'

interface AgentStatusBarProps {
  viewingMeta: SessionObservationMeta
  interactionMode: SessionInteractionMode
}

export function AgentStatusBar({
  viewingMeta,
  interactionMode,
}: AgentStatusBarProps) {
  const status = interactionMode === 'proxy' ? 'Attached' : 'Observing'

  return (
    <div className="agent-status-bar">
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
      <span className="agent-status-bar__status">{status}</span>
    </div>
  )
}
