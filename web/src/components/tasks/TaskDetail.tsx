import { useState, useEffect, useCallback } from 'react'
import './task-execution.css'
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
import { cn } from '../../lib/utils'

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

const BACKDROP_CLS = 'fixed inset-0 z-[90] bg-[var(--surface-scrim)]'
const PANEL_CLS =
  'fixed right-0 top-0 z-[100] flex h-full w-[420px] max-w-[90vw] translate-x-full flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-secondary)] transition-transform duration-[250ms] ease-in-out'
const PANEL_OPEN_CLS = 'translate-x-0'
const LOADING_CLS = 'flex flex-1 items-center justify-center text-[length:calc(var(--font-size-base)*0.9)] text-[var(--text-muted)]'

const HEADER_CLS = 'border-b border-[var(--border)] px-5 py-4'
const HEADER_TOP_CLS = 'mb-2 flex items-center justify-between'
const REF_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]'
const CLOSE_BTN_CLS =
  'flex h-8 w-8 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:h-11 pointer-coarse:w-11'
const PARENT_LINK_CLS =
  'mb-[0.3rem] inline-block cursor-pointer border-0 bg-transparent p-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--accent)] hover:underline'
const TITLE_CLS = 'mb-[0.6rem] text-[length:calc(var(--font-size-base)*1.05)] font-semibold leading-[1.4]'
const BADGES_CLS = 'flex flex-wrap gap-[0.4rem]'

const META_CLS = 'border-b border-[var(--border)] px-5 py-3'
const META_ROW_CLS = 'flex items-baseline justify-between py-[0.3rem] text-[length:calc(var(--font-size-base)*0.8)]'
const META_LABEL_CLS = 'mr-3 shrink-0 text-[var(--text-muted)]'
const META_VALUE_CLS = 'text-right text-[var(--text-secondary)]'
const META_VALUE_MONO_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.75)]'

const LABELS_CLS = 'flex flex-wrap justify-end gap-[0.3rem]'
const LABEL_PILL_CLS =
  'rounded-full bg-[color-mix(in_srgb,var(--color-info)_10%,transparent)] px-[0.4rem] py-[0.1rem] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--color-info)]'

const SECTION_CLS = 'border-b border-[var(--border)] px-5 py-3'
const SECTION_TITLE_CLS =
  'mb-[0.4rem] text-[length:calc(var(--font-size-base)*0.75)] font-semibold uppercase tracking-[0.05em] text-[var(--text-muted)]'
const DESCRIPTION_CLS =
  'whitespace-pre-wrap text-[length:calc(var(--font-size-base)*0.85)] leading-[1.6] text-[var(--text-secondary)]'
const CRITERIA_CLS = 'rounded-md bg-[var(--bg-tertiary)] px-3 py-2 text-[length:calc(var(--font-size-base)*0.8)]'

const STATUS_CHANGE_CLS = 'flex flex-col gap-2 border-b border-[var(--border)] px-5 py-3'
const STATUS_FIELD_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none pointer-coarse:min-h-11'
const STATUS_REASON_CLS = 'min-h-[4.5rem] resize-y'
const STATUS_SUBMIT_CLS =
  'self-end cursor-pointer rounded-md border border-[var(--accent)] bg-[var(--accent)] px-4 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

const DEP_LIST_CLS = 'flex flex-wrap gap-[0.3rem]'
const DEP_ITEM_CLS =
  'inline-block cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-[0.15rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--accent)] hover:bg-[var(--border)] pointer-coarse:min-h-11'

const PROGRESS_CLS = 'mb-2 flex items-center gap-2'
const PROGRESS_BAR_CLS = 'h-1.5 flex-1 overflow-hidden rounded-[3px] bg-[var(--bg-tertiary)]'
const PROGRESS_FILL_CLS = 'h-full rounded-[3px] bg-[var(--color-success-foreground)]'
const PROGRESS_PCT_CLS = 'min-w-10 text-right text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'

const SUBTASK_LIST_CLS = 'flex flex-col gap-[0.15rem]'
const SUBTASK_ITEM_CLS =
  'flex cursor-pointer items-center gap-[0.4rem] rounded border-0 bg-transparent px-[0.4rem] py-[0.3rem] text-left font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] transition-colors duration-100 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const SUBTASK_REF_CLS = 'shrink-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const SUBTASK_TITLE_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap'

const VALIDATION_STATUS_CLS = 'mb-2 flex items-center gap-2'
const VALIDATION_BADGE_CLS =
  'inline-flex items-center rounded px-2 py-[0.15rem] text-[length:calc(var(--font-size-base)*0.75)] font-semibold'
const VALIDATION_FAILS_CLS = 'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const VALIDATION_CRITERIA_WRAP_CLS = 'mb-2'
const VALIDATION_FEEDBACK_WRAP_CLS = 'mt-2'
const VALIDATION_LABEL_CLS =
  'mb-1 block text-[length:calc(var(--font-size-base)*0.7)] uppercase tracking-[0.03em] text-[var(--text-muted)]'

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

  const blockerIds = deps?.blockers?.map(b => b.id) || []
  const blockingIds = deps?.blocking?.map(b => b.id) || []

  const closedCount = subtasks.filter(t => {
    const bucket = getTaskBucket(t)
    return bucket === 'closed' || bucket === 'merge_ready'
  }).length
  const progressPct = subtasks.length > 0 ? Math.round((closedCount / subtasks.length) * 100) : 0

  return (
    <>
      {isOpen && <div className={BACKDROP_CLS} onClick={onClose} />}

      <div className={cn(PANEL_CLS, isOpen && PANEL_OPEN_CLS)}>
        {isLoading ? (
          <div className={LOADING_CLS}>Loading...</div>
        ) : task ? (
          <>
            <div className={HEADER_CLS}>
              <div className={HEADER_TOP_CLS}>
                <span className={REF_CLS}>{task.ref}</span>
                <button className={CLOSE_BTN_CLS} onClick={onClose} title="Close">
                  <CloseIcon />
                </button>
              </div>
              {task.parent_task_id && (
                <button
                  className={PARENT_LINK_CLS}
                  onClick={() => onSelectTask(task.parent_task_id!)}
                >
                  ← Parent task
                </button>
              )}
              <h3 className={TITLE_CLS}>{task.title}</h3>
              <div className={BADGES_CLS}>
                <TaskStateBadges task={task} />
                <PriorityBadge priority={task.priority} />
                <TypeBadge type={task.task_type} />
              </div>
            </div>

            <StatusChange
              task={task}
              actions={actions}
              loading={actionLoading}
              onAction={handleAction}
            />

            {taskState?.is_escalated && (
              <EscalationCard
                task={task}
                targetStatus={getDeEscalationTargetStatus(task)}
                onResolve={() => {
                  void fetchDetail(task.id)
                }}
              />
            )}

            <div className={META_CLS}>
              <MetaRow label="Created" value={formatDate(task.created_at)} />
              <MetaRow label="Updated" value={formatDate(task.updated_at)} />
              {task.closed_at && <MetaRow label="Closed" value={formatDate(task.closed_at)} />}
              {task.closed_reason && <MetaRow label="Close reason" value={task.closed_reason} />}
              {task.labels && task.labels.length > 0 && (
                <div className={META_ROW_CLS}>
                  <span className={META_LABEL_CLS}>Labels</span>
                  <div className={LABELS_CLS}>
                    {task.labels.map((l, i) => (
                      <span key={`${l}-${i}`} className={LABEL_PILL_CLS}>{l}</span>
                    ))}
                  </div>
                </div>
              )}
            </div>

            <div className={SECTION_CLS}>
              <PermissionOverrides taskId={task.id} />
            </div>

            <div className={SECTION_CLS}>
              <h4 className={SECTION_TITLE_CLS}>Timeline</h4>
              <ReasoningTimeline
                task={task}
                onIntervene={(_phaseKey, action) => {
                  if (action === 'rollback' || action === 'retry') {
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

            {task.created_in_session_id && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Actions</h4>
                <ActionFeed sessionId={task.created_in_session_id} />
              </div>
            )}

            {task.created_in_session_id && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Session</h4>
                <SessionViewer sessionId={task.created_in_session_id} />
              </div>
            )}

            {task.created_in_session_id && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Capabilities</h4>
                <CapabilityScope sessionId={task.created_in_session_id} />
              </div>
            )}

            {blockerIds.length > 0 && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Blocked By</h4>
                <div className={DEP_LIST_CLS}>
                  {blockerIds.map(id => (
                    <button key={id} className={DEP_ITEM_CLS} onClick={() => onSelectTask(id)}>
                      {id.slice(0, 8)}...
                    </button>
                  ))}
                </div>
              </div>
            )}

            {blockingIds.length > 0 && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Blocks</h4>
                <div className={DEP_LIST_CLS}>
                  {blockingIds.map(id => (
                    <button key={id} className={DEP_ITEM_CLS} onClick={() => onSelectTask(id)}>
                      {id.slice(0, 8)}...
                    </button>
                  ))}
                </div>
              </div>
            )}

            {subtasks.length > 0 && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>
                  Subtasks ({closedCount}/{subtasks.length})
                </h4>
                <div className={PROGRESS_CLS}>
                  <div className={PROGRESS_BAR_CLS}>
                    <div className={PROGRESS_FILL_CLS} style={{ width: `${progressPct}%` }} />
                  </div>
                  <span className={PROGRESS_PCT_CLS}>{progressPct}%</span>
                </div>
                <div className={SUBTASK_LIST_CLS}>
                  {subtasks.map(st => (
                    <button key={st.id} className={SUBTASK_ITEM_CLS} onClick={() => onSelectTask(st.id)}>
                      <StatusDot task={st} />
                      <span className={SUBTASK_REF_CLS}>{st.ref}</span>
                      <span className={SUBTASK_TITLE_CLS}>{st.title}</span>
                    </button>
                  ))}
                </div>
              </div>
            )}

            {task.description && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Description</h4>
                <div className={DESCRIPTION_CLS}>{task.description}</div>
              </div>
            )}

            <div className={SECTION_CLS}>
              <h4 className={SECTION_TITLE_CLS}>Comments</h4>
              <TaskComments taskId={task.id} />
            </div>

            {(task.validation_criteria || task.validation_status !== 'pending') && (
              <ValidationSection task={task} />
            )}

            <div className={SECTION_CLS}>
              <h4 className={SECTION_TITLE_CLS}>Results</h4>
              <TaskResults task={task} />
            </div>

            {task.created_in_session_id && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Usage</h4>
                <TokenTracker sessionId={task.created_in_session_id} />
              </div>
            )}

            {task.created_in_session_id && (
              <div className={SECTION_CLS}>
                <h4 className={SECTION_TITLE_CLS}>Memories</h4>
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
    <div className={META_ROW_CLS}>
      <span className={META_LABEL_CLS}>{label}</span>
      <span className={cn(META_VALUE_CLS, mono && META_VALUE_MONO_CLS)}>{value}</span>
    </div>
  )
}

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
    <div className={SECTION_CLS}>
      <h4 className={SECTION_TITLE_CLS}>Validation</h4>

      <div className={VALIDATION_STATUS_CLS}>
        <span
          className={VALIDATION_BADGE_CLS}
          style={{ color: style.color, background: style.bg }}
        >
          {style.label}
        </span>
        {(task.validation_fail_count ?? 0) > 0 && (
          <span className={VALIDATION_FAILS_CLS}>
            {task.validation_fail_count ?? 0} failure{(task.validation_fail_count ?? 0) !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {task.validation_criteria && (
        <div className={VALIDATION_CRITERIA_WRAP_CLS}>
          <span className={VALIDATION_LABEL_CLS}>Criteria</span>
          <div className={cn(DESCRIPTION_CLS, CRITERIA_CLS)}>
            {task.validation_criteria}
          </div>
        </div>
      )}

      {task.validation_feedback && (
        <div className={VALIDATION_FEEDBACK_WRAP_CLS}>
          <span className={VALIDATION_LABEL_CLS}>Feedback</span>
          <div className={DESCRIPTION_CLS}>
            {task.validation_feedback}
          </div>
        </div>
      )}
    </div>
  )
}


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
    <div className={STATUS_CHANGE_CLS}>
      <select
        className={STATUS_FIELD_CLS}
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
        className={cn(STATUS_FIELD_CLS, STATUS_REASON_CLS)}
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder={reasonRequired ? 'Reason (required)' : 'Reason (optional)'}
        rows={3}
        disabled={loading}
      />
      <button
        type="button"
        className={STATUS_SUBMIT_CLS}
        onClick={() => { void submit() }}
        disabled={submitDisabled}
      >
        Update
      </button>
    </div>
  )
}
