import { memo, useCallback, useMemo, useState } from 'react'
import type { ToolCall } from '../../types/chat'
import { cn } from '../../lib/utils'
import { TOOL_CARD_SPACING } from '../shared/spacing'
import { Badge } from '../ui/Badge'
import { DropdownCaret } from '../ui/DropdownCaret'
import { JsonBlock } from './JsonBlock'
import { RichContentBlocks } from './RichContentBlocks'
import {
  COMPACT_HEADER_NAMES,
  COMPACT_HEADER_TOOL_TYPES,
  defaultExpandedForCall,
  FILE_TOOL_TYPES,
  getToolDisplayName,
  getToolSummary,
  groupToolCalls,
  hasVisibleToolCall,
  pathBasename,
  resolveToolType,
  type ToolCallGroup,
} from './ToolCallCard.helpers'
import {
  ToolArgumentsContent,
  ToolErrorBody,
  ToolLocations,
  ToolResultContent,
} from './ToolCallCardContent'
import { AskUserQuestionCard, ToolApprovalCard } from './ToolCallCardInteractions'

interface ToolCallCardProps {
  toolCalls: ToolCall[]
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (
    toolCallId: string,
    decision: 'approve' | 'reject' | 'approve_always',
  ) => boolean | void
}

const ToolCallItem = memo(function ToolCallItem({
  call,
  onRespond,
  onRespondToApproval,
  nested = false,
}: {
  call: ToolCall
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (
    toolCallId: string,
    decision: 'approve' | 'reject' | 'approve_always',
  ) => boolean | void
  nested?: boolean
}) {
  const displayName = getToolDisplayName(call)
  const toolType = resolveToolType(call)
  const [expanded, setExpanded] = useState(defaultExpandedForCall(call))
  const summary = getToolSummary(call)
  const isCompact =
    summary !== null &&
    (COMPACT_HEADER_TOOL_TYPES.has(toolType) || COMPACT_HEADER_NAMES.has(displayName))
  const isFileHeader = FILE_TOOL_TYPES.has(toolType)

  if (call.tool_name === 'AskUserQuestion') {
    return <AskUserQuestionCard call={call} onRespond={onRespond} />
  }
  if (call.status === 'pending_approval') {
    return <ToolApprovalCard call={call} onRespondToApproval={onRespondToApproval} />
  }

  const hasDetails =
    call.arguments ||
    call.result != null ||
    call.error ||
    Boolean(call.content_blocks?.length) ||
    Boolean(call.locations?.length) ||
    call.raw_output != null

  return (
    <div
      className={cn(
        '@container',
        nested
          ? 'border-b border-border last:border-b-0 overflow-hidden'
          : 'rounded-lg border border-border overflow-hidden my-1.5',
        call.status === 'error' && 'border-destructive-foreground/30',
      )}
    >
      <div
        className={cn(
          TOOL_CARD_SPACING.header,
          'text-sm cursor-pointer hover:bg-muted/50 transition-colors',
        )}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={() => hasDetails && setExpanded(!expanded)}
        onKeyDown={event => {
          if (
            event.target === event.currentTarget &&
            hasDetails &&
            (event.key === 'Enter' || event.key === ' ')
          ) {
            event.preventDefault()
            setExpanded(!expanded)
          }
        }}
      >
        <StatusIcon status={call.status} />
        <span className="font-mono text-foreground">{displayName}</span>
        {call.tool_kind && (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[length:var(--text-xs)] text-muted-foreground">
            {call.tool_kind}
          </span>
        )}
        {summary && isFileHeader ? (
          <>
            <span className="text-muted-foreground text-xs truncate hidden @sm:inline">
              {summary}
            </span>
            <span className="text-muted-foreground text-xs truncate @sm:hidden">
              {pathBasename(summary)}
            </span>
          </>
        ) : summary ? (
          <span className="text-muted-foreground text-xs truncate max-w-[12rem] @sm:max-w-[24rem]">
            {summary}
          </span>
        ) : null}
        <div className="flex-1" />
        {hasDetails && <DropdownCaret open={expanded} />}
      </div>
      {expanded && hasDetails && (
        <div className={cn(TOOL_CARD_SPACING.body, 'text-xs')}>
        {call.arguments && Object.keys(call.arguments).length > 0 && !isCompact && (
          <ToolArgumentsContent args={call.arguments} callId={call.id} />
        )}
        {call.locations && call.locations.length > 0 && (
          <ToolLocations callId={call.id} locations={call.locations} />
        )}
          {call.content_blocks && call.content_blocks.length > 0 && (
            <div className="min-w-0 max-w-full overflow-hidden">
              <div className={cn('text-muted-foreground', TOOL_CARD_SPACING.label)}>
                Content
              </div>
              <RichContentBlocks
                blocks={call.content_blocks}
                idPrefix={`tool-content-${call.id}`}
              />
            </div>
          )}
          {call.status === 'completed' && call.result != null && toolType !== 'edit' && (
            <div className="min-w-0 max-w-full overflow-hidden">
              <div className={cn('text-muted-foreground', TOOL_CARD_SPACING.label)}>
                Result
              </div>
              <ToolResultContent call={call} />
            </div>
          )}
          {call.raw_output != null && (
            <div className="min-w-0 max-w-full overflow-hidden">
              <div className={cn('text-muted-foreground', TOOL_CARD_SPACING.label)}>
                Raw Output
              </div>
              <JsonBlock value={call.raw_output} />
            </div>
          )}
          {call.status === 'error' && call.error && <ToolErrorBody error={call.error} />}
        </div>
      )}
    </div>
  )
})

function StatusIcon({ status }: { status: string }) {
  if (status === 'calling') {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-accent animate-spin"
        aria-label="In flight"
        role="img"
      >
        <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="16" />
      </svg>
    )
  }
  if (status === 'completed') {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-success-foreground"
        aria-label="Completed"
        role="img"
      >
        <polyline points="20 6 9 17 4 12" />
      </svg>
    )
  }
  if (status === 'error') {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-destructive-foreground"
        aria-label="Errors"
        role="img"
      >
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    )
  }
  if (status === 'pending' || status === 'pending_approval') {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-warning-foreground"
        aria-label="Pending"
        role="img"
      >
        <circle cx="12" cy="12" r="10" />
        <polyline points="12 6 12 12 16 14" />
      </svg>
    )
  }
  return null
}

function GroupStatusIcon({
  hasErrors,
  allCompleted,
  hasInFlight,
}: {
  hasErrors: boolean
  allCompleted: boolean
  hasInFlight: boolean
}) {
  if (hasInFlight) {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.25"
        className="text-accent animate-spin"
        aria-label="In flight"
        role="img"
      >
        <circle cx="12" cy="12" r="10" strokeDasharray="32" strokeDashoffset="16" />
      </svg>
    )
  }
  if (hasErrors) {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.75"
        className="text-destructive-foreground"
        aria-label="Errors"
        role="img"
      >
        <line x1="18" y1="6" x2="6" y2="18" />
        <line x1="6" y1="6" x2="18" y2="18" />
      </svg>
    )
  }
  if (allCompleted) {
    return (
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        className="text-success-foreground"
        aria-label="Completed"
        role="img"
      >
        <polyline points="20 6 9 17 4 12" />
      </svg>
    )
  }
  return null
}

function ToolCallGroupHeader({
  group,
  expanded,
  onToggle,
  onRespond,
  onRespondToApproval,
}: {
  group: ToolCallGroup
  expanded: boolean
  onToggle: () => void
  onRespond?: (toolCallId: string, answers: Record<string, string>) => boolean | void
  onRespondToApproval?: (
    toolCallId: string,
    decision: 'approve' | 'reject' | 'approve_always',
  ) => boolean | void
}) {
  const serverName = group.tool_calls[0]?.server_name
  const groupBorderClass = group.hasErrors
    ? 'border-destructive-foreground/50'
    : group.hasInFlight
      ? 'border-accent/50'
      : group.allCompleted
        ? 'border-success-foreground/40'
        : 'border-border'

  return (
    <div className={cn('border-l my-1', groupBorderClass)}>
      <div
        className="flex items-center gap-2 pl-3 pr-2 py-1 text-sm cursor-pointer hover:bg-muted/30 transition-colors"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        onClick={onToggle}
        onKeyDown={event => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            onToggle()
          }
        }}
      >
        <GroupStatusIcon
          hasErrors={group.hasErrors}
          allCompleted={group.allCompleted}
          hasInFlight={group.hasInFlight}
        />
        <span className="font-mono text-foreground">{group.displayName}</span>
        <Badge variant="default">×{group.tool_calls.length}</Badge>
        {serverName && serverName !== 'builtin' && (
          <span className="text-muted-foreground text-xs">{serverName}</span>
        )}
        <div className="flex-1" />
        <DropdownCaret open={expanded} />
      </div>
      {expanded && (
        <div className="pl-3">
          {group.tool_calls.map(call => (
            <ToolCallItem
              key={call.id}
              call={call}
              nested
              onRespond={onRespond}
              onRespondToApproval={onRespondToApproval}
            />
          ))}
        </div>
      )}
    </div>
  )
}

export const ToolCallCards = memo(function ToolCallCards({
  toolCalls,
  onRespond,
  onRespondToApproval,
}: ToolCallCardProps) {
  const visibleToolCalls = useMemo(() => toolCalls.filter(hasVisibleToolCall), [toolCalls])
  const segments = useMemo(() => groupToolCalls(visibleToolCalls), [visibleToolCalls])
  const [groupExpansionOverrides, setGroupExpansionOverrides] = useState<
    Record<string, boolean>
  >({})

  const toggleGroup = useCallback((key: string, defaultExpanded: boolean) => {
    setGroupExpansionOverrides(previous => {
      const current = previous[key]
      return { ...previous, [key]: current == null ? !defaultExpanded : !current }
    })
  }, [])

  if (!visibleToolCalls.length) return null

  return (
    <div className="my-1">
      {segments.map(segment => {
        if (segment.kind === 'single') {
          return (
            <ToolCallItem
              key={segment.call.id}
              call={segment.call}
              onRespond={onRespond}
              onRespondToApproval={onRespondToApproval}
            />
          )
        }
        const groupKey = `${segment.tool_calls[0].id}-${segment.toolName}`
        const defaultExpanded = segment.displayName !== 'Protocol' || segment.hasInFlight
        const expanded = groupExpansionOverrides[groupKey] ?? defaultExpanded
        return (
          <ToolCallGroupHeader
            key={groupKey}
            group={segment}
            expanded={expanded}
            onToggle={() => toggleGroup(groupKey, defaultExpanded)}
            onRespond={onRespond}
            onRespondToApproval={onRespondToApproval}
          />
        )
      })}
    </div>
  )
})
