import { useEffect, useState } from 'react'
import type {
  ApprovalOption,
  ContextUsage,
  SessionInteractionMode,
  SessionObservationMeta,
} from '../../types/chat'
import { cn } from '../../lib/utils'
import { Button } from '../ui/Button'
import { Chip } from '../ui/Chip'
import { chipIdentityClasses } from '../ui/chipVariants'
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

function getSessionKindBadge(
  sessionType: SessionObservationMeta['sessionType'],
): string | null {
  if (sessionType === 'web_chat') {
    return 'WEB'
  }
  if (sessionType === 'terminal') {
    return 'TMUX'
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
      className={cn(
        'agent-status-bar flex min-h-[var(--activity-panel-bar-height,2.5rem)] shrink-0 items-center justify-between gap-3 border-t border-border bg-[var(--bg-secondary)] px-3 @max-[360px]/chat-column:pl-3 @max-[360px]/chat-column:pr-2',
        planPendingApproval && 'agent-status-bar--pending flex-wrap gap-y-1.5',
      )}
      data-testid="agent-status-bar"
    >
      <div className="agent-status-bar__summary flex min-w-0 flex-1 flex-wrap items-center justify-start gap-3 mobile:gap-2 @max-[360px]/chat-column:flex-nowrap">
        {viewingMeta && stateText ? (
          <div className="chat-session-status flex min-w-0 flex-wrap items-center gap-1.5 @max-[360px]/chat-column:flex-nowrap">
            <span className="chat-session-status__state whitespace-nowrap text-[length:var(--text-sm)] font-medium leading-none text-[var(--text-muted)] @max-[360px]/chat-column:hidden">
              {stateText}
            </span>
            {sessionBadge ? (
              <Chip tone="accent" uppercase className={chipIdentityClasses}>
                {sessionBadge}
              </Chip>
            ) : null}
          </div>
        ) : null}
        <div className="agent-status-bar__context flex shrink-0 items-center">
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
      <div className="agent-status-bar__actions flex shrink-0 items-center gap-1.5">
        {canAttach && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            onClick={onAttach}
            aria-label="Attach"
            title="Attach"
          >
            <LinkIcon />
            <span className="chat-action-btn__label @max-[479px]/chat-column:hidden">Attach</span>
          </Button>
        )}
        {canDetach && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            onClick={onDetach}
            aria-label="Detach"
            title="Detach"
          >
            <UnlinkIcon />
            <span className="chat-action-btn__label @max-[479px]/chat-column:hidden">Detach</span>
          </Button>
        )}
        {onOpenTerminal && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            onClick={onOpenTerminal}
            aria-label="Terminal"
            title="Terminal"
          >
            <PromptIcon />
            <span className="chat-action-btn__label @max-[479px]/chat-column:hidden">Terminal</span>
          </Button>
        )}
        {canResume && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            onClick={onResume}
            aria-label="Resume"
            title="Resume"
          >
            <PlayIcon />
            <span className="chat-action-btn__label @max-[479px]/chat-column:hidden">Resume</span>
          </Button>
        )}
        <Button
          type="button"
          variant="accent"
          size="sm"
          dense
          className="chat-new-chat-btn"
          onClick={onNewChat}
          disabled={!onNewChat}
          aria-label="New Chat"
          title="New Chat"
        >
          <PlusIcon />
          <span className="chat-new-chat-btn__label @max-[479px]/chat-column:hidden">New Chat</span>
        </Button>
      </div>
      {planPendingApproval && onApprovePlan && onRequestPlanChanges && (
        <div className="agent-status-bar__plan min-w-0 basis-full [&_button]:h-[var(--status-bar-control-height)] [&_button]:min-h-[var(--status-bar-control-height)]">
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
