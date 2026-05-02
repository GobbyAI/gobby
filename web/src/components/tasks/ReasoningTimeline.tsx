import { useState } from 'react'
import type { GobbyTaskDetail } from '../../hooks/useTasks'
import { relativeTime } from '../../utils/formatTime'
import { getCanonicalTaskState, getTaskBucket } from '../../lib/taskState'

interface TimelinePhase {
  key: string
  icon: string
  label: string
  status: 'complete' | 'active' | 'pending'
  timestamp: string | null
  summary: string | null
}

const ROOT_CLS = 'flex flex-col'
const PHASE_CLS = 'flex min-h-9 gap-2.5'
const LINE_CLS = 'flex w-4 shrink-0 flex-col items-center'
const DOT_CLS =
  'relative mt-1 h-2.5 w-2.5 shrink-0 rounded-full border-2 border-[var(--border)] bg-[var(--bg-primary)]'
const DOT_COMPLETE_CLS = 'border-[var(--accent)] bg-[var(--accent)]'
const DOT_ACTIVE_CLS = 'border-[var(--accent)] bg-[var(--accent)]'
const DOT_PENDING_CLS = 'border-[var(--border)] bg-[var(--bg-tertiary)]'
const DOT_PULSE_CLS =
  'absolute -inset-1 rounded-full border-2 border-[var(--accent)] [animation:reasoning-pulse_2s_ease-in-out_infinite]'
const CONNECTOR_CLS = 'min-h-3 w-0.5 flex-1 bg-[var(--border)]'
const CONNECTOR_COMPLETE_CLS = 'bg-[var(--accent)]'
const CONTENT_CLS = 'min-w-0 flex-1 pb-2'
const HEADER_CLS =
  'flex w-full cursor-pointer items-center gap-1.5 border-none bg-transparent py-0.5 text-left text-[length:var(--text-md)] text-[var(--text-primary)] disabled:cursor-default'
const HEADER_PENDING_CLS = 'text-[var(--text-muted)]'
const PHASE_ICON_CLS = 'text-[length:var(--text-base)] leading-none'
const LABEL_CLS = 'font-medium'
const TIME_CLS =
  'ml-auto font-[inherit] text-[length:var(--text-xs)] text-[var(--text-muted)]'
const CHEVRON_CLS = 'w-3 text-center text-[length:var(--text-xs)] text-[var(--text-muted)]'
const DETAIL_CLS = 'pb-0.5 pl-5 pt-1 text-[length:var(--text-sm)] leading-[1.4] text-[var(--text-secondary)]'
const INTERVENTIONS_CLS = 'mt-2 flex flex-wrap gap-1.5'
const BTN_CLS =
  'inline-flex cursor-pointer items-center gap-1 rounded border px-2.5 py-[3px] font-[var(--font-sans)] text-[length:var(--text-xs)] transition-colors duration-150'
const BTN_DEFAULT_CLS =
  'border-[var(--border)] bg-[var(--bg-secondary)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]'
const BTN_PRIMARY_CLS =
  'border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[var(--color-info-soft)] text-[var(--color-info)] hover:bg-[color-mix(in_srgb,var(--color-info)_22%,transparent)]'
const BTN_DANGER_CLS =
  'border-[color-mix(in_srgb,var(--color-error)_25%,transparent)] bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)] text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_20%,transparent)]'
const BTN_ICON_CLS = 'text-[length:var(--text-sm)]'

const TIMELINE_BUCKET_ORDER = ['ready', 'in_progress', 'review', 'merge_ready', 'closed'] as const

function getTimelineBucketIndex(task: GobbyTaskDetail): number {
  const state = getCanonicalTaskState(task)
  const bucket = getTaskBucket(task)

  if (bucket !== 'blocked') {
    return TIMELINE_BUCKET_ORDER.indexOf(bucket)
  }

  if (state.is_merge_ready) return TIMELINE_BUCKET_ORDER.indexOf('merge_ready')
  if (state.lifecycle_stage === 'needs_review') return TIMELINE_BUCKET_ORDER.indexOf('review')
  if (state.is_claimed || state.lifecycle_stage === 'in_progress') {
    return TIMELINE_BUCKET_ORDER.indexOf('in_progress')
  }

  return TIMELINE_BUCKET_ORDER.indexOf('ready')
}

function derivePhases(task: GobbyTaskDetail): TimelinePhase[] {
  const state = getCanonicalTaskState(task)
  const bucketIdx = getTimelineBucketIndex(task)
  const isFailed = state.is_escalated

  const phases: TimelinePhase[] = []

  phases.push({
    key: 'plan',
    icon: '\u{1F4CB}',
    label: 'Plan',
    status: 'complete',
    timestamp: task.created_at,
    summary: `Task created: ${task.title}`,
  })

  const investigateReached = bucketIdx >= 1 || isFailed
  phases.push({
    key: 'investigate',
    icon: '\u{1F50D}',
    label: 'Investigate',
    status: investigateReached
      ? (bucketIdx === 1 && !isFailed ? 'active' : 'complete')
      : 'pending',
    timestamp: investigateReached ? task.updated_at : null,
    summary: state.owner_session_id
      ? `Assigned to ${task.agent_name || state.owner_session_id}`
      : investigateReached ? 'Work started' : null,
  })

  const actReached = bucketIdx >= 2 || (task.commits && task.commits.length > 0) || isFailed
  phases.push({
    key: 'act',
    icon: '\u{2699}\u{FE0F}',
    label: 'Act',
    status: actReached
      ? (bucketIdx === 2 && !isFailed ? 'active' : 'complete')
      : 'pending',
    timestamp: actReached ? task.updated_at : null,
    summary: task.commits && task.commits.length > 0
      ? `${task.commits.length} commit${task.commits.length > 1 ? 's' : ''} linked`
      : actReached ? 'Changes submitted' : null,
  })

  const verifyReached = bucketIdx >= 3 || task.validation_status !== 'pending' || isFailed
  let verifySummary: string | null = null
  if (isFailed) {
    verifySummary = `Escalated: ${task.escalation_reason || 'needs attention'}`
  } else if (task.validation_status === 'passed' || task.validation_status === 'valid') {
    verifySummary = 'Validation passed'
  } else if (task.validation_status === 'failed') {
    verifySummary = `Validation failed: ${task.validation_feedback || 'see feedback'}`
  } else if (task.closed_at) {
    verifySummary = `Closed: ${task.closed_reason || 'completed'}`
  }

  phases.push({
    key: 'verify',
    icon: '\u{2705}',
    label: 'Verify',
    status: verifyReached
      ? (
          state.is_closed || state.is_merge_ready || task.validation_status === 'passed' || task.validation_status === 'valid'
            ? 'complete'
            : 'active'
        )
      : 'pending',
    timestamp: task.closed_at || (verifyReached ? task.updated_at : null),
    summary: verifySummary,
  })

  return phases
}

type InterventionAction = 'retry' | 'edit_and_run' | 'rollback' | 'mark_resolved'

interface InterventionButton {
  action: InterventionAction
  label: string
  icon: string
  variant: 'default' | 'primary' | 'danger'
}

function getInterventionsForPhase(
  phase: TimelinePhase,
  task: GobbyTaskDetail,
): InterventionButton[] {
  const isFailed = getCanonicalTaskState(task).is_escalated

  if (phase.status === 'pending') return []

  if (phase.status === 'active') {
    const buttons: InterventionButton[] = [
      { action: 'mark_resolved', label: 'Mark Resolved', icon: '✔', variant: 'primary' },
    ]
    if (phase.key !== 'plan') {
      buttons.push({ action: 'retry', label: 'Retry', icon: '↻', variant: 'default' })
    }
    return buttons
  }

  const buttons: InterventionButton[] = []

  if (isFailed && phase.key === 'verify') {
    buttons.push({ action: 'retry', label: 'Retry', icon: '↻', variant: 'primary' })
    buttons.push({ action: 'mark_resolved', label: 'Mark Resolved', icon: '✔', variant: 'default' })
  } else if (phase.key !== 'plan') {
    buttons.push({ action: 'rollback', label: 'Roll Back', icon: '↩', variant: 'danger' })
    buttons.push({ action: 'retry', label: 'Retry', icon: '↻', variant: 'default' })
  }

  return buttons
}

const DOT_VARIANT: Record<TimelinePhase['status'], string> = {
  complete: DOT_COMPLETE_CLS,
  active: DOT_ACTIVE_CLS,
  pending: DOT_PENDING_CLS,
}

const BTN_VARIANT: Record<InterventionButton['variant'], string> = {
  default: BTN_DEFAULT_CLS,
  primary: BTN_PRIMARY_CLS,
  danger: BTN_DANGER_CLS,
}

interface ReasoningTimelineProps {
  task: GobbyTaskDetail
  onIntervene?: (phaseKey: string, action: InterventionAction) => void
}

export function ReasoningTimeline({ task, onIntervene }: ReasoningTimelineProps) {
  const phases = derivePhases(task)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggle = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className={ROOT_CLS}>
      {phases.map((phase, i) => {
        const isExpanded = expanded.has(phase.key)
        const isLast = i === phases.length - 1

        return (
          <div key={phase.key} className={PHASE_CLS}>
            <div className={LINE_CLS}>
              <span className={`${DOT_CLS} ${DOT_VARIANT[phase.status]}`}>
                {phase.status === 'active' && <span className={DOT_PULSE_CLS} />}
              </span>
              {!isLast && (
                <span
                  className={
                    phase.status === 'complete'
                      ? `${CONNECTOR_CLS} ${CONNECTOR_COMPLETE_CLS}`
                      : CONNECTOR_CLS
                  }
                />
              )}
            </div>
            <div className={CONTENT_CLS}>
              <button
                className={phase.status === 'pending' ? `${HEADER_CLS} ${HEADER_PENDING_CLS}` : HEADER_CLS}
                onClick={() => phase.summary && toggle(phase.key)}
                disabled={!phase.summary}
              >
                <span className={PHASE_ICON_CLS}>{phase.icon}</span>
                <span className={LABEL_CLS}>{phase.label}</span>
                {phase.timestamp && (
                  <span className={TIME_CLS}>{relativeTime(phase.timestamp)}</span>
                )}
                {phase.summary && (
                  <span className={CHEVRON_CLS}>{isExpanded ? '▾' : '▸'}</span>
                )}
              </button>
              {isExpanded && phase.summary && (
                <div className={DETAIL_CLS}>
                  {phase.summary}
                  {onIntervene && (() => {
                    const buttons = getInterventionsForPhase(phase, task)
                    if (buttons.length === 0) return null
                    return (
                      <div className={INTERVENTIONS_CLS}>
                        {buttons.map(btn => (
                          <button
                            key={btn.action}
                            className={`${BTN_CLS} ${BTN_VARIANT[btn.variant]}`}
                            onClick={(e) => {
                              e.stopPropagation()
                              onIntervene(phase.key, btn.action)
                            }}
                          >
                            <span className={BTN_ICON_CLS}>{btn.icon}</span>
                            {btn.label}
                          </button>
                        ))}
                      </div>
                    )
                  })()}
                </div>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
