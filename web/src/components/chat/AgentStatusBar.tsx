import { useSyncExternalStore } from 'react'
import type { ContextUsage, SessionInteractionMode, SessionObservationMeta } from '../../types/chat'
import { ContextUsageIndicator } from './ContextUsageIndicator'
import { LinkIcon, PlayIcon, UnlinkIcon } from '../icons'
import { PlusIcon } from './icons/PlusIcon'

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
  onNewChat?: () => void
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
  onNewChat,
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

  return (
    <div className="agent-status-bar" data-testid="agent-status-bar">
      <div className="agent-status-bar__summary">
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
        {!isAttached && (
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
        )}
      </div>
      <div className="agent-status-bar__actions">
        {canAttach && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onAttach}
          >
            <LinkIcon />
            Attach
          </button>
        )}
        {canResume && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onResume}
          >
            <PlayIcon />
            Resume
          </button>
        )}
        {canDetach && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onDetach}
          >
            <UnlinkIcon />
            Detach
          </button>
        )}
        <button
          type="button"
          className="btn btn-accent btn-sm"
          onClick={onNewChat}
          disabled={!onNewChat}
        >
          <PlusIcon />
          New Chat
        </button>
      </div>
    </div>
  )
}
