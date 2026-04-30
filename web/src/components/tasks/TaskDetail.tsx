import { useState, useEffect, useCallback } from 'react'
import './task-detail.css'
import './task-execution.css'
import './task-advanced.css'
import type { GobbyTask, GobbyTaskDetail, DependencyTree } from '../../hooks/useTasks'
import { PriorityBadge, TypeBadge, StatusDot, TaskStateBadges } from './TaskBadges'
import { ReasoningTimeline } from './ReasoningTimeline'
import { ActionFeed } from './ActionFeed'
import { SessionViewer } from './SessionViewer'
import { CapabilityScope } from './CapabilityScope'
import { EscalationCard } from './EscalationCard'
import { TaskResults } from './TaskResults'
import { TokenTracker } from './TokenTracker'
import { TaskMemories } from './TaskMemories'
import { TaskComments } from './TaskComments'
import { PermissionOverrides } from './PermissionOverrides'
import { getCanonicalTaskState, getTaskBucket } from '../../lib/taskState'

interface TaskActions {
  claimTask: (id: string, sessionId: string, force?: boolean) => Promise<GobbyTaskDetail | null>
  releaseTaskClaim: (id: string, status?: string) => Promise<GobbyTaskDetail | null>
  markTaskNeedsReview: (id: string, notes?: string) => Promise<GobbyTaskDetail | null>
  markTaskReviewApproved: (id: string, notes?: string) => Promise<GobbyTaskDetail | null>
  escalateTask: (id: string, reason: string) => Promise<GobbyTaskDetail | null>
  deEscalateTask: (
    id: string,
    decisionContext: string,
    targetStatus?: string,
    resetValidation?: boolean
  ) => Promise<GobbyTaskDetail | null>
  closeTask: (id: string, reason?: string) => Promise<GobbyTaskDetail | null>
  reopenTask: (id: string, reason?: string) => Promise<GobbyTaskDetail | null>
}

interface TaskDetailProps {
  taskId: string | null
  getTask: (id: string) => Promise<GobbyTaskDetail | null>
  getDependencies: (id: string) => Promise<DependencyTree | null>
  getSubtasks: (id: string) => Promise<GobbyTask[]>
  actions: TaskActions
  onSelectTask: (id: string) => void
  onClose: () => void
}

function CloseIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function formatDate(iso: string | null): string {
  if (!iso) return '-'
  const d = new Date(iso)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function getDeEscalationTargetStatus(task: GobbyTaskDetail): string {
  return task.pre_escalation_status ?? 'open'
}

export function TaskDetail({ taskId, getTask, getDependencies, getSubtasks, actions, onSelectTask, onClose }: TaskDetailProps) {
  const [task, setTask] = useState<GobbyTaskDetail | null>(null)
  const [deps, setDeps] = useState<DependencyTree | null>(null)
  const [subtasks, setSubtasks] = useState<GobbyTask[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)

  const fetchDetail = useCallback(async (id: string) => {
    setIsLoading(true)
    try {
      const [result, depTree, children] = await Promise.all([
        getTask(id),
        getDependencies(id),
        getSubtasks(id),
      ])
      setTask(result)
      setDeps(depTree)
      setSubtasks(children)
    } catch (e) {
      console.error('Failed to fetch task detail:', e)
    } finally {
      setIsLoading(false)
    }
  }, [getTask, getDependencies, getSubtasks])

  useEffect(() => {
    if (taskId) {
      fetchDetail(taskId)
    } else {
      setTask(null)
      setDeps(null)
      setSubtasks([])
    }
  }, [taskId, fetchDetail])

  const handleAction = useCallback(async (action: () => Promise<GobbyTaskDetail | null>) => {
    setActionLoading(true)
    try {
      const updated = await action()
      if (updated) setTask(updated)
    } catch (e) {
      console.error('Failed to perform action:', e)
    } finally {
      setActionLoading(false)
    }
  }, [])

  const taskState = task ? getCanonicalTaskState(task) : null

  const isOpen = taskId !== null

  // Collect flat blocker/blocking IDs from tree
  const blockerIds = deps?.blockers?.map(b => b.id) || []
  const blockingIds = deps?.blocking?.map(b => b.id) || []

  // Subtask progress
  const closedCount = subtasks.filter(t => {
    const bucket = getTaskBucket(t)
    return bucket === 'closed' || bucket === 'merge_ready'
  }).length
  const progressPct = subtasks.length > 0 ? Math.round((closedCount / subtasks.length) * 100) : 0

  return (
    <>
      {isOpen && <div className="task-detail-backdrop" onClick={onClose} />}

      <div className={`task-detail-panel ${isOpen ? 'open' : ''}`}>
        {isLoading ? (
          <div className="task-detail-loading">Loading...</div>
        ) : task ? (
          <>
            {/* Header */}
            <div className="task-detail-header">
              <div className="task-detail-header-top">
                <span className="task-detail-ref">{task.ref}</span>
                <button className="task-detail-close" onClick={onClose} title="Close">
                  <CloseIcon />
                </button>
              </div>
              {/* Parent breadcrumb */}
              {task.parent_task_id && (
                <button
                  className="task-detail-parent-link"
                  onClick={() => onSelectTask(task.parent_task_id!)}
                >
                  ← Parent task
                </button>
              )}
              <h3 className="task-detail-title">{task.title}</h3>
              <div className="task-detail-badges">
                <TaskStateBadges task={task} />
                <PriorityBadge priority={task.priority} />
                <TypeBadge type={task.task_type} />
              </div>
            </div>

            {/* Status change */}
            <StatusChange
              task={task}
              actions={actions}
              loading={actionLoading}
              onAction={handleAction}
            />

            {/* Escalation card (shown prominently when task is escalated) */}
            {taskState?.is_escalated && (
              <EscalationCard
                task={task}
                targetStatus={getDeEscalationTargetStatus(task)}
                onResolve={() => {
                  void fetchDetail(task.id)
                }}
              />
            )}

            {/* Metadata */}
            <div className="task-detail-meta">
              <MetaRow label="Created" value={formatDate(task.created_at)} />
              <MetaRow label="Updated" value={formatDate(task.updated_at)} />
              {task.closed_at && <MetaRow label="Closed" value={formatDate(task.closed_at)} />}
              {task.closed_reason && <MetaRow label="Close reason" value={task.closed_reason} />}
              {task.labels && task.labels.length > 0 && (
                <div className="task-detail-meta-row">
                  <span className="task-detail-meta-label">Labels</span>
                  <div className="task-detail-labels">
                    {task.labels.map((l, i) => (
                      <span key={`${l}-${i}`} className="task-detail-label">{l}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>

            {/* Permission Overrides */}
            <div className="task-detail-section">
              <PermissionOverrides taskId={task.id} />
            </div>

            {/* Reasoning Timeline */}
            <div className="task-detail-section">
              <h4 className="task-detail-section-title">Timeline</h4>
              <ReasoningTimeline
                task={task}
                onIntervene={(_phaseKey, action) => {
                  if (action === 'rollback' || action === 'retry') {
                    // Roll back / retry: reopen the task so work can restart
                    handleAction(() => actions.reopenTask(task.id))
                  } else if (action === 'mark_resolved') {
                    if (getCanonicalTaskState(task).is_escalated) {
                      handleAction(() =>
                        actions.deEscalateTask(task.id, 'Resolved from reasoning timeline', 'open')
                      )
                    } else {
                      handleAction(() => actions.markTaskNeedsReview(task.id))
                    }
                  } else if (action === 'edit_and_run') {
                    handleAction(() => actions.reopenTask(task.id))
                  }
                }}
              />
            </div>

            {/* Action Feed */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Actions</h4>
                <ActionFeed sessionId={task.created_in_session_id} />
              </div>
            )}

            {/* Session Transcript */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Session</h4>
                <SessionViewer sessionId={task.created_in_session_id} />
              </div>
            )}

            {/* Capability Scope */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Capabilities</h4>
                <CapabilityScope sessionId={task.created_in_session_id} />
              </div>
            )}

            {/* Dependencies: Blocked By */}
            {blockerIds.length > 0 && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Blocked By</h4>
                <div className="task-detail-dep-list">
                  {blockerIds.map(id => (
                    <button key={id} className="task-detail-dep-item" onClick={() => onSelectTask(id)}>
                      {id.slice(0, 8)}...
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Dependencies: Blocks */}
            {blockingIds.length > 0 && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Blocks</h4>
                <div className="task-detail-dep-list">
                  {blockingIds.map(id => (
                    <button key={id} className="task-detail-dep-item" onClick={() => onSelectTask(id)}>
                      {id.slice(0, 8)}...
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Subtasks */}
            {subtasks.length > 0 && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">
                  Subtasks ({closedCount}/{subtasks.length})
                </h4>
                <div className="task-detail-progress">
                  <div className="task-detail-progress-bar">
                    <div className="task-detail-progress-fill" style={{ width: `${progressPct}%` }} />
                  </div>
                  <span className="task-detail-progress-pct">{progressPct}%</span>
                </div>
                <div className="task-detail-subtask-list">
                  {subtasks.map(st => (
                    <button key={st.id} className="task-detail-subtask-item" onClick={() => onSelectTask(st.id)}>
                      <StatusDot task={st} />
                      <span className="task-detail-subtask-ref">{st.ref}</span>
                      <span className="task-detail-subtask-title">{st.title}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {/* Description */}
            {task.description && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Description</h4>
                <div className="task-detail-description">{task.description}</div>
              </div>
            )}

            {/* Comments */}
            <div className="task-detail-section">
              <h4 className="task-detail-section-title">Comments</h4>
              <TaskComments taskId={task.id} />
            </div>

            {/* Validation */}
            {(task.validation_criteria || task.validation_status !== 'pending') && (
              <ValidationSection task={task} />
            )}

            {/* Results (outcome, commits, PR links) */}
            <div className="task-detail-section">
              <h4 className="task-detail-section-title">Results</h4>
              <TaskResults task={task} />
            </div>

            {/* Token Usage */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Usage</h4>
                <TokenTracker sessionId={task.created_in_session_id} />
              </div>
            )}

            {/* Memories */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Memories</h4>
                <TaskMemories sessionId={task.created_in_session_id} />
              </div>
            )}
          </>
        ) : null}
      </div>
    </>
  )
}

function MetaRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="task-detail-meta-row">
      <span className="task-detail-meta-label">{label}</span>
      <span className={`task-detail-meta-value ${mono ? 'mono' : ''}`}>{value}</span>
    </div>
  )
}

// =============================================================================
// Validation section
// =============================================================================

const VALIDATION_STATUS_STYLES: Record<string, { color: string; bg: string; label: string }> = {
  pending: { color: 'var(--text-muted)', bg: 'color-mix(in srgb, var(--text-muted) 15%, transparent)', label: 'Pending' },
  passed: { color: 'var(--color-success-foreground)', bg: 'var(--color-success-soft)', label: 'Passed' },
  failed: { color: 'var(--color-error)', bg: 'var(--color-error-soft)', label: 'Failed' },
  skipped: { color: 'var(--color-warning-foreground)', bg: 'var(--color-warning-soft)', label: 'Skipped' },
}

function ValidationSection({ task }: { task: GobbyTaskDetail }) {
  const vstatus = task.validation_status || 'pending'
  const style = VALIDATION_STATUS_STYLES[vstatus] || VALIDATION_STATUS_STYLES.pending

  return (
    <div className="task-detail-section">
      <h4 className="task-detail-section-title">Validation</h4>

      {/* Status indicator */}
      <div className="task-detail-validation-status">
        <span
          className="task-detail-validation-badge"
          style={{ color: style.color, background: style.bg }}
        >
          {style.label}
        </span>
        {(task.validation_fail_count ?? 0) > 0 && (
          <span className="task-detail-validation-fails">
            {task.validation_fail_count ?? 0} failure{(task.validation_fail_count ?? 0) !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Criteria */}
      {task.validation_criteria && (
        <div className="task-detail-validation-criteria">
          <span className="task-detail-validation-criteria-label">Criteria</span>
          <div className="task-detail-description task-detail-criteria">
            {task.validation_criteria}
          </div>
        </div>
      )}

      {/* Feedback */}
      {task.validation_feedback && (
        <div className="task-detail-validation-feedback">
          <span className="task-detail-validation-criteria-label">Feedback</span>
          <div className="task-detail-description">
            {task.validation_feedback}
          </div>
        </div>
      )}
    </div>
  )
}


// =============================================================================
// Status change (single dropdown + reason textarea + Update button)
// =============================================================================

interface StatusOption {
  value: string
  label: string
  reasonRequired: boolean
  call: (reason: string) => Promise<GobbyTaskDetail | null>
}

function getStatusOptions(task: GobbyTaskDetail, actions: TaskActions): StatusOption[] {
  const { id } = task
  const state = getCanonicalTaskState(task)
  const options: StatusOption[] = []

  if (state.is_closed) {
    options.push({
      value: 'reopen',
      label: 'Reopen',
      reasonRequired: false,
      call: (reason) => actions.reopenTask(id, reason || undefined),
    })
    return options
  }

  if (state.is_escalated) {
    options.push({
      value: 'resume',
      label: 'Resume',
      reasonRequired: false,
      call: (reason) =>
        actions.deEscalateTask(
          id,
          reason || 'Resumed from task detail',
          getDeEscalationTargetStatus(task),
        ),
    })
  } else {
    if (state.is_claimed) {
      options.push({
        value: 'release_claim',
        label: 'Release Claim',
        reasonRequired: false,
        call: () => actions.releaseTaskClaim(id),
      })
    }
    if (state.lifecycle_stage !== 'needs_review' && !state.is_merge_ready) {
      options.push({
        value: 'needs_review',
        label: 'Send to Review',
        reasonRequired: false,
        call: (reason) => actions.markTaskNeedsReview(id, reason || undefined),
      })
    }
    if (state.lifecycle_stage === 'needs_review') {
      options.push({
        value: 'review_approved',
        label: 'Approve Review',
        reasonRequired: false,
        call: (reason) => actions.markTaskReviewApproved(id, reason || undefined),
      })
    }
    options.push({
      value: 'blocked',
      label: 'Mark Blocked',
      reasonRequired: true,
      call: (reason) => actions.escalateTask(id, reason),
    })
  }

  options.push({
    value: 'closed',
    label: 'Close',
    reasonRequired: false,
    call: (reason) => actions.closeTask(id, reason || undefined),
  })

  return options
}

function StatusChange({
  task,
  actions,
  loading,
  onAction,
}: {
  task: GobbyTaskDetail
  actions: TaskActions
  loading: boolean
  onAction: (action: () => Promise<GobbyTaskDetail | null>) => Promise<void>
}) {
  const options = getStatusOptions(task, actions)
  const [target, setTarget] = useState('')
  const [reason, setReason] = useState('')

  if (options.length === 0) return null

  const selectedOption = options.find((opt) => opt.value === target)
  const reasonRequired = selectedOption?.reasonRequired ?? false
  const submitDisabled =
    loading || !selectedOption || (reasonRequired && reason.trim().length === 0)

  const submit = async () => {
    if (!selectedOption) return
    if (reasonRequired && reason.trim().length === 0) return
    const trimmed = reason.trim()
    await onAction(() => selectedOption.call(trimmed))
    setTarget('')
    setReason('')
  }

  return (
    <div className="task-detail-status-change">
      <select
        className="task-detail-status-select"
        value={target}
        onChange={(e) => setTarget(e.target.value)}
        disabled={loading}
        aria-label="Change status"
      >
        <option value="" disabled>
          Change status to…
        </option>
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
      <textarea
        className="task-detail-status-reason"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder={reasonRequired ? 'Reason (required)' : 'Reason (optional)'}
        rows={3}
        disabled={loading}
      />
      <button
        type="button"
        className="task-detail-status-submit"
        onClick={() => { void submit() }}
        disabled={submitDisabled}
      >
        Update
      </button>
    </div>
  )
}
