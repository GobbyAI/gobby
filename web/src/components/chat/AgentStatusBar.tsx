import { useSyncExternalStore } from 'react'
import type { ContextUsage, SessionInteractionMode, SessionObservationMeta } from '../../types/chat'
import { ContextUsageIndicator } from './ContextUsageIndicator'

interface AgentStatusBarProps {
  viewingMeta?: SessionObservationMeta | null
  interactionMode: SessionInteractionMode
  contextUsage?: ContextUsage
  contextUsageUpdatedAt?: number | null
  isAttached?: boolean
  isAutonomousSession?: boolean
  onAttach?: () => void
  onResume?: () => void
  onDetach?: () => void
  onTogglePanel?: () => void
  isPanelPinned?: boolean
}

const CONTEXT_USAGE_REFRESH_MS = 15_000

function subscribeToClock(onStoreChange: () => void): () => void {
  const interval = window.setInterval(onStoreChange, CONTEXT_USAGE_REFRESH_MS)
  return () => window.clearInterval(interval)
}

function subscribeToClockDisabled(): () => void {
  return () => {}
}

function getClockSnapshot(): number {
  return Date.now()
}

function getSessionKindBadge(sessionType: SessionObservationMeta['sessionType']): {
  label: string
  className: string
} | null {
  if (sessionType === 'web_chat') {
    return { label: 'WEB', className: 'chip--web' }
  }
  if (sessionType === 'terminal') {
    return { label: 'TMUX', className: 'chip--tmux' }
  }

  return null
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
  contextUsage,
  contextUsageUpdatedAt = null,
  isAttached = false,
  isAutonomousSession = false,
  onAttach,
  onResume,
  onDetach,
  onTogglePanel,
  isPanelPinned = false,
}: AgentStatusBarProps) {
  const usageClock = useSyncExternalStore(
    contextUsageUpdatedAt == null ? subscribeToClockDisabled : subscribeToClock,
    getClockSnapshot,
    getClockSnapshot,
  )

  const contextUsageStaleMs =
    contextUsageUpdatedAt != null ? Math.max(0, usageClock - contextUsageUpdatedAt) : null
  const sessionBadge = viewingMeta ? getSessionKindBadge(viewingMeta.sessionType) : null
  const stateText = viewingMeta ? formatSessionStateText(interactionMode, isAttached) : null
  const canAttach = !isAttached && !isAutonomousSession && Boolean(onAttach)
  const canResume = !isAttached && !isAutonomousSession && Boolean(onResume)
  const canDetach = isAttached && Boolean(onDetach)
  const hasActions =
    Boolean(onTogglePanel) ||
    canAttach ||
    canResume ||
    canDetach

  return (
    <div className="agent-status-bar" data-testid="agent-status-bar">
      <div className="agent-status-bar__summary">
        <div className="agent-status-bar__context">
          <ContextUsageIndicator
            totalInputTokens={contextUsage?.totalInputTokens ?? 0}
            outputTokens={contextUsage?.outputTokens ?? 0}
            contextWindow={contextUsage?.contextWindow ?? null}
            staleMs={contextUsageStaleMs}
            uncachedInputTokens={contextUsage?.uncachedInputTokens ?? 0}
            cacheReadTokens={contextUsage?.cacheReadTokens ?? 0}
            cacheCreationTokens={contextUsage?.cacheCreationTokens ?? 0}
          />
        </div>
        {viewingMeta && stateText ? (
          <div className="chat-session-status">
            <span className="chat-session-status__state">{stateText}</span>
            {sessionBadge ? (
              <span className={`chip ${sessionBadge.className}`}>
                {sessionBadge.label}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      {hasActions ? (
        <div className="agent-status-bar__actions">
          {onTogglePanel && (
            <button
              type="button"
              className="btn btn-ghost btn-icon btn-sm"
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
              className="btn btn-secondary btn-sm"
              onClick={onAttach}
            >
              Attach
            </button>
          )}
          {!isAttached && !isAutonomousSession && onResume && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={onResume}
            >
              Resume
            </button>
          )}
          {isAttached && onDetach && (
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={onDetach}
            >
              Detach
            </button>
          )}
        </div>
      ) : null}
    </div>
  )
}
