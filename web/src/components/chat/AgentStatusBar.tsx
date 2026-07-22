import { useEffect, useState } from 'react'
import type {
  ApprovalOption,
  ContextUsage,
  SessionInteractionMode,
  SessionObservationMeta,
} from '../../types/chat'
import { cn } from '../../lib/utils'
import { ContextUsageIndicator } from './ContextUsageIndicator'
import { LinkIcon, PlayIcon, UnlinkIcon } from '../icons'
import { PlusIcon } from './icons/PlusIcon'
import { PlanPendingActionStrip } from './PlanPendingActionStrip'
import type { PlanPendingVariant } from './planPendingSurface'

function PromptIcon() {
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
      aria-hidden="true"
    >
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  )
}

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
  onOpenTerminal?: () => void
  onNewChat?: () => void
  planPendingApproval?: boolean
  planApprovalOptions?: ApprovalOption[]
  onApprovePlan?: (option?: ApprovalOption) => void
  onRequestPlanChanges?: (feedback: string) => void
  onViewPlan?: () => void
  planPendingVariant?: PlanPendingVariant
}

const CONTEXT_USAGE_REFRESH_MS = 15_000

function subscribeToClock(onStoreChange: () => void): () => void {
  const interval = window.setInterval(onStoreChange, CONTEXT_USAGE_REFRESH_MS)
  return () => window.clearInterval(interval)
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
  onOpenTerminal,
  onNewChat,
  planPendingApproval = false,
  planApprovalOptions,
  onApprovePlan,
  onRequestPlanChanges,
  onViewPlan,
  planPendingVariant,
}: AgentStatusBarProps) {
  const [usageClock, setUsageClock] = useState(() => Date.now())

  useEffect(() => {
    if (contextUsageUpdatedAt == null) return undefined

    return subscribeToClock(() => setUsageClock(Date.now()))
  }, [contextUsageUpdatedAt])

  const contextUsageStaleMs =
    contextUsageUpdatedAt != null ? Math.max(0, usageClock - contextUsageUpdatedAt) : null
  const sessionBadge = viewingMeta ? getSessionKindBadge(viewingMeta.sessionType) : null
  const stateText = viewingMeta ? formatSessionStateText(interactionMode, isAttached) : null
  const canAttach = !isAttached && Boolean(onAttach)
  const canResume = !isAutonomousSession && Boolean(onResume)
  const canDetach = isAttached && Boolean(onDetach)

  return (
    <div
      className={cn('agent-status-bar', planPendingApproval && 'agent-status-bar--pending')}
      data-testid="agent-status-bar"
    >
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
        <div className="agent-status-bar__context">
          <ContextUsageIndicator
            totalInputTokens={contextUsage?.totalInputTokens ?? 0}
            outputTokens={contextUsage?.outputTokens ?? 0}
            contextWindow={contextUsage?.contextWindow ?? null}
            contextUsageRatio={contextUsage?.contextUsageRatio ?? null}
            staleMs={contextUsageStaleMs}
            uncachedInputTokens={contextUsage?.uncachedInputTokens ?? 0}
            cacheReadTokens={contextUsage?.cacheReadTokens ?? 0}
            cacheCreationTokens={contextUsage?.cacheCreationTokens ?? 0}
          />
        </div>
      </div>
      <div className="agent-status-bar__actions">
        {canAttach && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onAttach}
            aria-label="Attach"
            title="Attach"
          >
            <LinkIcon />
            <span className="chat-action-btn__label">Attach</span>
          </button>
        )}
        {canDetach && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onDetach}
            aria-label="Detach"
            title="Detach"
          >
            <UnlinkIcon />
            <span className="chat-action-btn__label">Detach</span>
          </button>
        )}
        {onOpenTerminal && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onOpenTerminal}
            aria-label="Terminal"
            title="Terminal"
          >
            <PromptIcon />
            <span className="chat-action-btn__label">Terminal</span>
          </button>
        )}
        {canResume && (
          <button
            type="button"
            className="btn btn-accent btn-sm"
            onClick={onResume}
            aria-label="Resume"
            title="Resume"
          >
            <PlayIcon />
            <span className="chat-action-btn__label">Resume</span>
          </button>
        )}
        <button
          type="button"
          className="btn btn-accent btn-sm chat-new-chat-btn"
          onClick={onNewChat}
          disabled={!onNewChat}
          aria-label="New Chat"
          title="New Chat"
        >
          <PlusIcon />
          <span className="chat-new-chat-btn__label">New Chat</span>
        </button>
      </div>
      {planPendingApproval && onApprovePlan && onRequestPlanChanges && (
        <div className="agent-status-bar__plan">
          <PlanPendingActionStrip
            onApprove={onApprovePlan}
            onRequestChanges={onRequestPlanChanges}
            onView={onViewPlan}
            options={planApprovalOptions}
            variant={planPendingVariant}
          />
        </div>
      )}
    </div>
  )
}
