import { useState, useEffect, useCallback } from 'react'
import './task-detail.css'
import './task-execution.css'
import './task-advanced.css'
import type { GobbyTask, GobbyTaskDetail, DependencyTree } from '../../hooks/useTasks'
import { StatusBadge, PriorityBadge, TypeBadge, StatusDot } from './TaskBadges'
import { ReasoningTimeline } from './ReasoningTimeline'
import { ActionFeed } from './ActionFeed'
import { SessionViewer } from './SessionViewer'
import { CapabilityScope } from './CapabilityScope'
import { RawTraceView } from './RawTraceView'
import { OversightSelector } from './OversightSelector'
import { EscalationCard } from './EscalationCard'
import { TaskResults } from './TaskResults'
import { CostTracker } from './CostTracker'
import { TaskMemories } from './TaskMemories'
import { AssigneePicker } from './AssigneePicker'
import { TaskComments } from './TaskComments'
import { PermissionOverrides } from './PermissionOverrides'
import { TaskHandoff } from './TaskHandoff'
import { LaunchAgentDialog } from './LaunchAgentDialog'

interface TaskActions {
  updateTask: (id: string, params: { status?: string; assignee?: string }) => Promise<GobbyTaskDetail | null>
  closeTask: (id: string, reason?: string) => Promise<GobbyTaskDetail | null>
  reopenTask: (id: string) => Promise<GobbyTaskDetail | null>
}

interface TaskDetailProps {
  taskId: string | null
  getTask: (id: string) => Promise<GobbyTaskDetail | null>
  getDependencies: (id: string) => Promise<DependencyTree | null>
  getSubtasks: (id: string) => Promise<GobbyTask[]>
  actions: TaskActions
  onSelectTask: (id: string) => void
  onClose: () => void
  onClone?: (task: GobbyTaskDetail) => void
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

export function TaskDetail({ taskId, getTask, getDependencies, getSubtasks, actions, onSelectTask, onClose, onClone }: TaskDetailProps) {
  const [task, setTask] = useState<GobbyTaskDetail | null>(null)
  const [deps, setDeps] = useState<DependencyTree | null>(null)
  const [subtasks, setSubtasks] = useState<GobbyTask[]>([])
  const [isLoading, setIsLoading] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [showLaunchAgent, setShowLaunchAgent] = useState(false)

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

  const isOpen = taskId !== null

  // Collect flat blocker/blocking IDs from tree
  const blockerIds = deps?.blockers?.map(b => b.id) || []
  const blockingIds = deps?.blocking?.map(b => b.id) || []

  // Subtask progress
  const closedCount = subtasks.filter(t => t.status === 'closed' || t.status === 'review_approved').length
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
                <StatusBadge status={task.status} />
                <PriorityBadge priority={task.priority} />
                <TypeBadge type={task.task_type} />
              </div>
            </div>

            {/* Actions */}
            <StatusActions
              task={task}
              actions={actions}
              loading={actionLoading}
              onAction={handleAction}
            />

            {/* Launch Agent */}
            {(task.status === 'open' || task.status === 'in_progress') && (
              <div className="task-detail-section">
                <button
                  className="task-detail-action-btn task-detail-action-btn--primary launch-agent-trigger"
                  onClick={() => setShowLaunchAgent(true)}
                >
                  Launch Agent
                </button>
                <LaunchAgentDialog
                  isOpen={showLaunchAgent}
                  taskId={task.id}
                  taskTitle={task.title}
                  taskCategory={task.category}
                  projectId={task.project_id}
                  onClose={() => setShowLaunchAgent(false)}
                  onSpawned={() => {
                    if (taskId) fetchDetail(taskId)
                  }}
                />
              </div>
            )}

            {/* Secondary actions */}
            {onClone && (
              <div className="task-detail-secondary-actions">
                <button
                  className="task-detail-clone-btn"
                  onClick={() => onClone(task)}
                  title="Clone this task with same fields"
                >
                  {'\u{1F4CB}'} Clone Task
                </button>
              </div>
            )}

            {/* Handoff */}
            {task.status !== 'closed' && (
              <div className="task-detail-section">
                <TaskHandoff
                  taskId={task.id}
                  currentAssignee={task.assignee}
                  onHandoff={async (assignee) => {
                    await handleAction(() => actions.updateTask(task.id, { assignee }))
                  }}
                />
              </div>
            )}

            {/* Escalation card (shown prominently when task is escalated) */}
            {task.status === 'escalated' && (
              <EscalationCard
                task={task}
                onResolve={() => {
                  handleAction(() => actions.reopenTask(task.id))
                }}
              />
            )}

            {/* Metadata */}
            <div className="task-detail-meta">
              <div className="task-detail-meta-row">
                <span className="task-detail-meta-label">Assignee</span>
                <AssigneePicker
                  currentAssignee={task.assignee}
                  currentAgentName={task.agent_name}
                  onAssign={(assignee) => {
                    handleAction(() => actions.updateTask(task.id, { assignee: assignee || '' }))
                  }}
                />
              </div>
              <MetaRow label="Created" value={formatDate(task.created_at)} />
              <MetaRow label="Updated" value={formatDate(task.updated_at)} />
              {task.closed_at && <MetaRow label="Closed" value={formatDate(task.closed_at)} />}
              {task.closed_reason && <MetaRow label="Close reason" value={task.closed_reason} />}
              {task.category && <MetaRow label="Category" value={task.category} />}
              {task.path_cache && <MetaRow label="Path" value={task.path_cache} mono />}
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

            {/* Oversight Mode */}
            <div className="task-detail-section">
              <OversightSelector taskId={task.id} />
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
                    // Mark resolved: advance toward close
                    if (task.status === 'escalated') {
                      handleAction(() => actions.reopenTask(task.id))
                    } else {
                      handleAction(() => actions.updateTask(task.id, { status: 'needs_review' }))
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

            {/* Raw Trace (Debug) */}
            {task.created_in_session_id && (
              <DebugTraceSection sessionId={task.created_in_session_id} />
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
                      <StatusDot status={st.status} />
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

            {/* Cost & Token Usage */}
            {task.created_in_session_id && (
              <div className="task-detail-section">
                <h4 className="task-detail-section-title">Usage</h4>
                <CostTracker sessionId={task.created_in_session_id} />
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
  pending: { color: '#737373', bg: 'rgba(115, 115, 115, 0.12)', label: 'Pending' },
  passed: { color: '#22c55e', bg: 'rgba(34, 197, 94, 0.12)', label: 'Passed' },
  failed: { color: '#ef4444', bg: 'rgba(239, 68, 68, 0.12)', label: 'Failed' },
  skipped: { color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.12)', label: 'Skipped' },
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
// Debug trace section (collapsed by default)
// =============================================================================

function DebugTraceSection({ sessionId }: { sessionId: string }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="task-detail-section">
      <button
        className="task-detail-debug-toggle"
        onClick={() => setOpen(!open)}
      >
        <span className="task-detail-debug-toggle-icon">{open ? '\u25BE' : '\u25B8'}</span>
        <span className="task-detail-debug-toggle-label">Debug Trace</span>
      </button>
      {open && <RawTraceView sessionId={sessionId} />}
    </div>
  )
}

// =============================================================================
// Status action buttons - contextual by current status
// =============================================================================

interface StatusAction {
  label: string
  variant: 'primary' | 'default' | 'danger'
  onClick: () => Promise<GobbyTaskDetail | null>
}

function getActionsForStatus(
  task: GobbyTaskDetail,
  actions: TaskActions
): StatusAction[] {
  const { status, id } = task

  switch (status) {
    case 'open':
      return [
        { label: 'Start Work', variant: 'primary', onClick: () => actions.updateTask(id, { status: 'in_progress' }) },
        { label: 'Close', variant: 'danger', onClick: () => actions.closeTask(id) },
      ]
    case 'in_progress':
      return [
        { label: 'Submit for Review', variant: 'primary', onClick: () => actions.updateTask(id, { status: 'needs_review' }) },
        { label: 'Close', variant: 'danger', onClick: () => actions.closeTask(id) },
      ]
    case 'needs_review':
      return [
        { label: 'Approve', variant: 'primary', onClick: () => actions.updateTask(id, { status: 'review_approved' }) },
        { label: 'Reopen', variant: 'default', onClick: () => actions.reopenTask(id) },
      ]
    case 'review_approved':
      return [
        { label: 'Close', variant: 'primary', onClick: () => actions.closeTask(id) },
        { label: 'Reopen', variant: 'default', onClick: () => actions.reopenTask(id) },
      ]
    case 'closed':
      return [
        { label: 'Reopen', variant: 'default', onClick: () => actions.reopenTask(id) },
      ]
    case 'escalated':
      return [
        { label: 'Reopen', variant: 'primary', onClick: () => actions.reopenTask(id) },
      ]
    default:
      return [
        { label: 'Close', variant: 'danger', onClick: () => actions.closeTask(id) },
      ]
  }
}

function StatusActions({
  task,
  actions,
  loading,
  onAction,
}: {
  task: GobbyTaskDetail
  actions: TaskActions
  loading: boolean
  onAction: (action: () => Promise<GobbyTaskDetail | null>) => void
}) {
  const statusActions = getActionsForStatus(task, actions)

  if (statusActions.length === 0) return null

  return (
    <div className="task-detail-actions">
      {statusActions.map(a => (
        <button
          key={a.label}
          className={`task-detail-action-btn task-detail-action-btn--${a.variant}`}
          onClick={() => onAction(a.onClick)}
          disabled={loading}
        >
          {a.label}
        </button>
      ))}
    </div>
  )
}
