import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useCronJobs } from '../hooks/useCronJobs'
import type { CronJob, CronRun, CreateCronJobRequest, UpdateCronJobRequest } from '../hooks/useCronJobs'
import { SidebarPanel } from './shared/SidebarPanel'
import { cn } from '../lib/utils'
import { RunHistoryTable } from './cron/RunHistoryTable'
import { formatRelativeTime } from './cron/formatters'
import { Heading } from './shared/Heading'

type ScheduleType = CronJob['schedule_type']
type ActionType = CronJob['action_type']
type ActionConfig = Record<string, unknown>

interface JobFormValues {
  name: string
  description: string
  scheduleType: ScheduleType
  cronExpr: string
  intervalSeconds: string
  timezone: string
  actionType: ActionType
  actionConfigStr: string
}

const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden'

const TOOLBAR_CLS = 'flex flex-wrap items-center gap-2 border-b border-[var(--border)] px-4 py-3'
const TOOLBAR_TITLE_CLS = 'm-0 min-w-0 flex-[1_1_auto] text-[length:var(--text-lg)] font-semibold'
const TOOLBAR_SEARCH_CLS =
  'min-h-9 min-w-0 flex-[1_1_200px] rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const TOOLBAR_SELECT_CLS =
  'min-h-9 cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] pointer-coarse:min-h-11'
const TOOLBAR_BTN_CLS =
  'flex h-9 w-9 cursor-pointer items-center justify-center rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-0 text-[var(--text-secondary)] hover:bg-surface-tint hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:h-11 pointer-coarse:w-11'
const TOOLBAR_CREATE_CLS = 'shrink-0'

const JOB_LIST_CLS = 'flex flex-1 flex-col gap-1 overflow-y-auto p-2'
const JOB_ITEM_CLS =
  'flex w-full cursor-pointer flex-col gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2.5 text-left font-[inherit] text-inherit transition-colors duration-100 hover:bg-surface-tint focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'
const JOB_ITEM_SELECTED_CLS = 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)]'
const JOB_ITEM_DISABLED_CLS = 'opacity-60'
const JOB_ITEM_HEADER_CLS = 'flex min-w-0 items-center gap-1.5'
const JOB_NAME_CLS =
  'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-md)] font-medium'
const JOB_STATUS_DOT_CLS = 'h-2 w-2 shrink-0 rounded-full'
const JOB_STATUS_DOT_BG: Record<string, string> = {
  active: 'bg-[var(--color-success-foreground)]',
  inactive: 'bg-[var(--text-muted)]',
  failing: 'bg-[var(--color-error)]',
}
const JOB_ITEM_META_CLS = 'flex items-center gap-2 text-[length:var(--text-xs)] text-[var(--text-secondary)]'

const ACTION_BADGE_CLS =
  'rounded-sm px-1 py-px text-[length:var(--text-2xs)] font-medium uppercase'
const ACTION_BADGE_BG: Record<string, string> = {
  shell:
    'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]',
  agent_spawn: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]',
  pipeline: 'bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] text-[var(--accent)]',
}

const EMPTY_CLS =
  'flex flex-1 flex-col items-center justify-center gap-3 px-4 py-8 text-center text-[var(--text-secondary)]'
const EMPTY_TITLE_CLS = 'm-0 text-[length:var(--text-lg)] text-[var(--text-primary)]'
const EMPTY_TEXT_CLS = 'm-0 text-[length:var(--text-md)]'

const DETAIL_CLS = 'p-4'
const DETAIL_HEADER_CLS = 'mb-5 flex flex-wrap items-start justify-between gap-3'
const DETAIL_TITLE_BLOCK_CLS = 'min-w-0 flex-1'
const DETAIL_DESCRIPTION_CLS = 'm-0 text-[length:var(--text-md)] text-[var(--text-secondary)]'
const DETAIL_ACTIONS_CLS = 'flex flex-wrap gap-2'

const BTN_CLS =
  'inline-flex min-h-8 cursor-pointer items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-surface-tint disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-[var(--bg-secondary)] pointer-coarse:min-h-11'
const BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--bg-primary)] hover:bg-[var(--accent)] hover:opacity-90 disabled:hover:bg-[var(--accent)]'
const BTN_DANGER_CLS =
  'border-[var(--color-error)] text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)]'

const INFO_GRID_CLS =
  'mb-6 grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(180px,1fr))]'
const INFO_CARD_CLS = 'rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3'
const INFO_LABEL_CLS =
  'mb-1 text-[length:var(--text-xs)] uppercase tracking-[0.5px] text-[var(--text-secondary)]'
const INFO_VALUE_CLS =
  'break-all text-[length:var(--text-base)] font-medium [&_code]:rounded-sm [&_code]:bg-[var(--bg-primary)] [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:font-[inherit] [&_code]:text-[length:var(--text-md)]'

const CONFIG_SECTION_CLS = 'mb-6'
const CONFIG_HEADING_CLS = 'm-0 mb-2 text-[length:var(--text-base)] font-semibold'
const CONFIG_PRE_CLS =
  'overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3 font-[inherit] text-[length:var(--text-sm)]'

const RUNS_SECTION_CLS = 'mb-6'
const RUNS_HEADING_CLS = 'm-0 mb-3 flex items-center gap-2 text-[length:var(--text-base)] font-semibold'
const RUNS_LOADING_EMPTY_CLS = 'p-4 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]'

const FORM_CLS = 'flex h-full flex-col'
const FORM_BODY_CLS = 'flex-1 overflow-y-auto p-4'
const FORM_ACTIONS_CLS =
  'flex justify-end gap-2 border-t border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3'
const FORM_GROUP_CLS = 'mb-3'
const FORM_LABEL_CLS = 'mb-1 block text-[length:var(--text-sm)] font-medium text-[var(--text-secondary)]'
const FORM_INPUT_CLS =
  'box-border min-h-9 w-full rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-md)] text-[var(--text-primary)] outline-none transition-colors duration-150 focus:border-[var(--accent)] pointer-coarse:min-h-11'
const FORM_TEXTAREA_CLS =
  'box-border min-h-[100px] w-full resize-y rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none transition-colors duration-150 focus:border-[var(--accent)]'
const FORM_JSON_CLS =
  'overflow-x-auto whitespace-pre-wrap bg-[var(--bg-tertiary)] font-mono leading-[1.5] [tab-size:2]'

function getDefaultActionConfig(actionType: ActionType): string {
  switch (actionType) {
    case 'shell':
      return '{\n  "command": "echo",\n  "args": ["hello"]\n}'
    case 'agent_spawn':
      return '{\n  "prompt": "...",\n  "provider": "claude",\n  "model": "sonnet"\n}'
    case 'pipeline':
      return '{\n  "pipeline_name": "my-pipeline",\n  "inputs": {}\n}'
    default:
      return '{}'
  }
}

function formatSchedule(job: CronJob): string {
  if (job.schedule_type === 'cron' && job.cron_expr) {
    return job.cron_expr
  }
  if (job.schedule_type === 'interval' && job.interval_seconds) {
    const s = job.interval_seconds
    if (s >= 3600) return `Every ${Math.floor(s / 3600)}h${s % 3600 ? ` ${Math.floor((s % 3600) / 60)}m` : ''}`
    if (s >= 60) return `Every ${Math.floor(s / 60)}m`
    return `Every ${s}s`
  }
  if (job.schedule_type === 'once' && job.run_at) {
    return `Once at ${new Date(job.run_at).toLocaleString()}`
  }
  return job.schedule_type
}

function getStatusDotKey(job: CronJob): string {
  if (!job.enabled) return 'inactive'
  if (job.consecutive_failures > 0) return 'failing'
  return 'active'
}

const DEFAULT_FORM_VALUES: JobFormValues = {
  name: '',
  description: '',
  scheduleType: 'cron',
  cronExpr: '0 7 * * *',
  intervalSeconds: '300',
  timezone: 'UTC',
  actionType: 'shell',
  actionConfigStr: '{\n  "command": "echo",\n  "args": ["hello"]\n}',
}

function jobToFormValues(job: CronJob): JobFormValues {
  return {
    name: job.name,
    description: job.description || '',
    scheduleType: job.schedule_type,
    cronExpr: job.cron_expr || '0 7 * * *',
    intervalSeconds: String(job.interval_seconds || 300),
    timezone: job.timezone,
    actionType: job.action_type,
    actionConfigStr: JSON.stringify(job.action_config, null, 2),
  }
}

function parseActionConfig(actionConfigStr: string): ActionConfig | null {
  try {
    const parsed = JSON.parse(actionConfigStr)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as ActionConfig
    }
    return null
  } catch {
    return null
  }
}

function formValuesToCreateRequest(
  v: JobFormValues,
  actionConfig: ActionConfig,
): CreateCronJobRequest {
  const req: CreateCronJobRequest = {
    name: v.name.trim(),
    action_type: v.actionType,
    action_config: actionConfig,
    schedule_type: v.scheduleType,
    timezone: v.timezone,
  }
  if (v.description.trim()) req.description = v.description.trim()
  if (v.scheduleType === 'cron') req.cron_expr = v.cronExpr
  if (v.scheduleType === 'interval') req.interval_seconds = parseInt(v.intervalSeconds, 10)
  return req
}

function formValuesToUpdateRequest(
  v: JobFormValues,
  actionConfig: ActionConfig,
): UpdateCronJobRequest {
  const req: UpdateCronJobRequest = {
    name: v.name.trim(),
    description: v.description.trim() || undefined,
    schedule_type: v.scheduleType,
    timezone: v.timezone,
    action_type: v.actionType,
    action_config: actionConfig,
  }
  if (v.scheduleType === 'cron') req.cron_expr = v.cronExpr
  if (v.scheduleType === 'interval') req.interval_seconds = parseInt(v.intervalSeconds, 10)
  return req
}

interface JobFormProps {
  initialValues?: JobFormValues
  submitLabel: string
  isSubmitting?: boolean
  onSubmit: (values: JobFormValues, actionConfig: ActionConfig) => void | Promise<void>
  onCancel: () => void
}

function JobForm({ initialValues, submitLabel, isSubmitting, onSubmit, onCancel }: JobFormProps) {
  const [values, setValues] = useState<JobFormValues>(initialValues || DEFAULT_FORM_VALUES)
  const parsedActionConfig = useMemo(
    () => parseActionConfig(values.actionConfigStr),
    [values.actionConfigStr],
  )

  const isFormValid = useMemo(() => {
    if (!values.name.trim()) return false
    if (!parsedActionConfig) return false
    if (values.scheduleType === 'cron' && !values.cronExpr.trim()) return false
    if (values.scheduleType === 'interval') {
      const parsed = parseInt(values.intervalSeconds, 10)
      if (isNaN(parsed) || parsed < 10) return false
    }
    return true
  }, [values, parsedActionConfig])

  const update = <K extends keyof JobFormValues>(key: K, value: JobFormValues[K]) => {
    setValues(prev => ({ ...prev, [key]: value }))
  }

  const handleActionTypeChange = (newType: ActionType) => {
    setValues(prev => ({
      ...prev,
      actionType: newType,
      actionConfigStr: getDefaultActionConfig(newType),
    }))
  }

  const handleSubmit = () => {
    if (!isFormValid || isSubmitting || !parsedActionConfig) return
    onSubmit(values, parsedActionConfig)
  }

  return (
    <div className={FORM_CLS}>
      <div className={FORM_BODY_CLS}>
        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-name">Name</label>
          <input
            id="cron-form-name"
            className={FORM_INPUT_CLS}
            value={values.name}
            onChange={e => update('name', e.target.value)}
            placeholder="My Scheduled Job"
            autoFocus
          />
        </div>

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-description">Description</label>
          <input
            id="cron-form-description"
            className={FORM_INPUT_CLS}
            value={values.description}
            onChange={e => update('description', e.target.value)}
            placeholder="Optional description"
          />
        </div>

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-schedule-type">Schedule Type</label>
          <select
            id="cron-form-schedule-type"
            className={FORM_INPUT_CLS}
            value={values.scheduleType}
            onChange={e => update('scheduleType', e.target.value as ScheduleType)}
          >
            <option value="cron">Cron Expression</option>
            <option value="interval">Fixed Interval</option>
            <option value="once">One-shot</option>
          </select>
        </div>

        {values.scheduleType === 'cron' && (
          <div className={FORM_GROUP_CLS}>
            <label className={FORM_LABEL_CLS} htmlFor="cron-form-cron-expr">Cron Expression</label>
            <input
              id="cron-form-cron-expr"
              className={FORM_INPUT_CLS}
              value={values.cronExpr}
              onChange={e => update('cronExpr', e.target.value)}
              placeholder="0 7 * * *"
            />
          </div>
        )}

        {values.scheduleType === 'interval' && (
          <div className={FORM_GROUP_CLS}>
            <label className={FORM_LABEL_CLS} htmlFor="cron-form-interval">Interval (seconds)</label>
            <input
              id="cron-form-interval"
              className={FORM_INPUT_CLS}
              type="number"
              value={values.intervalSeconds}
              onChange={e => update('intervalSeconds', e.target.value)}
              min="10"
            />
          </div>
        )}

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-timezone">Timezone</label>
          <input
            id="cron-form-timezone"
            className={FORM_INPUT_CLS}
            value={values.timezone}
            onChange={e => update('timezone', e.target.value)}
            placeholder="UTC"
          />
        </div>

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-action-type">Action Type</label>
          <select
            id="cron-form-action-type"
            className={FORM_INPUT_CLS}
            value={values.actionType}
            onChange={e => handleActionTypeChange(e.target.value as ActionType)}
          >
            <option value="shell">Shell Command</option>
            <option value="agent_spawn">Agent Spawn</option>
            <option value="pipeline">Pipeline</option>
          </select>
        </div>

        <div className={FORM_GROUP_CLS}>
          <label className={FORM_LABEL_CLS} htmlFor="cron-form-action-config">Action Config (JSON)</label>
          <textarea
            id="cron-form-action-config"
            className={cn(FORM_TEXTAREA_CLS, FORM_JSON_CLS)}
            value={values.actionConfigStr}
            onChange={e => update('actionConfigStr', e.target.value)}
            rows={6}
          />
        </div>
      </div>

      <div className={FORM_ACTIONS_CLS}>
        <button type="button" className={BTN_CLS} onClick={onCancel}>Cancel</button>
        <button
          type="button"
          className={cn(BTN_CLS, BTN_PRIMARY_CLS)}
          onClick={handleSubmit}
          disabled={!isFormValid || isSubmitting}
        >
          {isSubmitting ? 'Saving...' : submitLabel}
        </button>
      </div>
    </div>
  )
}

interface JobDetailProps {
  job: CronJob
  runs: CronRun[]
  isRunsLoading: boolean
  onToggle: () => void
  onRunNow: () => void
  onEdit: () => void
  onDelete: () => void
  onNavigateToPipelineExecution?: (executionId: string) => void
}

function JobDetail({
  job,
  runs,
  isRunsLoading,
  onToggle,
  onRunNow,
  onEdit,
  onDelete,
  onNavigateToPipelineExecution,
}: JobDetailProps) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <div className={DETAIL_CLS}>
      <div className={DETAIL_HEADER_CLS}>
        <div className={DETAIL_TITLE_BLOCK_CLS}>
          {job.description && <p className={DETAIL_DESCRIPTION_CLS}>{job.description}</p>}
        </div>
        <div className={DETAIL_ACTIONS_CLS}>
          <button className={cn(BTN_CLS, BTN_PRIMARY_CLS)} onClick={onRunNow}>Run Now</button>
          <button className={BTN_CLS} onClick={onEdit}>Edit</button>
          <button className={BTN_CLS} onClick={onToggle}>
            {job.enabled ? 'Disable' : 'Enable'}
          </button>
          {confirmDelete ? (
            <>
              <button className={cn(BTN_CLS, BTN_DANGER_CLS)} onClick={onDelete}>Confirm</button>
              <button className={BTN_CLS} onClick={() => setConfirmDelete(false)}>Cancel</button>
            </>
          ) : (
            <button className={cn(BTN_CLS, BTN_DANGER_CLS)} onClick={() => setConfirmDelete(true)}>Delete</button>
          )}
        </div>
      </div>

      <div className={INFO_GRID_CLS}>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Schedule</div>
          <div className={INFO_VALUE_CLS}>
            <code>{formatSchedule(job)}</code>
          </div>
        </div>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Timezone</div>
          <div className={INFO_VALUE_CLS}>{job.timezone}</div>
        </div>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Status</div>
          <div className={INFO_VALUE_CLS}>
            {job.enabled ? 'Active' : 'Disabled'}
            {job.consecutive_failures > 0 && ` (${job.consecutive_failures} failures)`}
          </div>
        </div>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Action Type</div>
          <div className={INFO_VALUE_CLS}>
            <span className={cn(ACTION_BADGE_CLS, ACTION_BADGE_BG[job.action_type] ?? '')}>
              {job.action_type}
            </span>
          </div>
        </div>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Next Run</div>
          <div className={INFO_VALUE_CLS}>
            {job.next_run_at ? new Date(job.next_run_at).toLocaleString() : '-'}
          </div>
        </div>
        <div className={INFO_CARD_CLS}>
          <div className={INFO_LABEL_CLS}>Last Run</div>
          <div className={INFO_VALUE_CLS}>
            {job.last_run_at ? formatRelativeTime(job.last_run_at) : 'Never'}
            {job.last_status && ` (${job.last_status})`}
          </div>
        </div>
      </div>

      <div className={CONFIG_SECTION_CLS}>
        <Heading level={4} className={CONFIG_HEADING_CLS}>Action Config</Heading>
        <pre className={CONFIG_PRE_CLS}>
          {JSON.stringify(job.action_config, null, 2)}
        </pre>
      </div>

      <div className={RUNS_SECTION_CLS}>
        <Heading level={4} className={RUNS_HEADING_CLS}>Recent Runs</Heading>
        <RunHistoryTable
          runs={runs}
          isLoading={isRunsLoading}
          onNavigateToPipelineExecution={onNavigateToPipelineExecution}
        />
      </div>
    </div>
  )
}

interface CronJobsPageProps {
  projectId?: string | null
  onNavigateToPipelineExecution?: (executionId: string) => void
}

export function CronJobsPage({ projectId, onNavigateToPipelineExecution }: CronJobsPageProps) {
  const {
    jobs, selectedJob, selectJob, runs, filters, setFilters,
    isLoading, isRunsLoading, createJob, updateJob, deleteJob, toggleJob, runNow, refresh,
  } = useCronJobs(projectId)

  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [editingJob, setEditingJob] = useState<CronJob | null>(null)
  const [isSubmittingCreate, setIsSubmittingCreate] = useState(false)
  const [isSubmittingEdit, setIsSubmittingEdit] = useState(false)
  const [toastMessage, setToastMessage] = useState<string | null>(null)
  const toastTimerRef = useRef<number | null>(null)

  const showToast = useCallback((message: string) => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current)
    }
    setToastMessage(message)
    toastTimerRef.current = window.setTimeout(() => {
      setToastMessage(null)
      toastTimerRef.current = null
    }, 3000)
  }, [])

  const dismissToast = useCallback(() => {
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current)
      toastTimerRef.current = null
    }
    setToastMessage(null)
  }, [])

  useEffect(() => {
    return () => {
      if (toastTimerRef.current !== null) {
        window.clearTimeout(toastTimerRef.current)
      }
    }
  }, [])

  const handleCreate = useCallback(async (values: JobFormValues, actionConfig: ActionConfig) => {
    setIsSubmittingCreate(true)
    try {
      const req = formValuesToCreateRequest(values, actionConfig)
      const job = await createJob(req)
      if (job) {
        setShowCreateDialog(false)
        selectJob(job)
      } else {
        console.error('Failed to create job')
        showToast('Failed to create job')
      }
    } catch (e) {
      console.error('Failed to create job:', e)
      showToast(e instanceof Error ? e.message : 'Failed to create job')
    } finally {
      setIsSubmittingCreate(false)
    }
  }, [createJob, selectJob, showToast])

  const handleEditSave = useCallback(async (values: JobFormValues, actionConfig: ActionConfig) => {
    if (!editingJob) return
    setIsSubmittingEdit(true)
    try {
      const req = formValuesToUpdateRequest(values, actionConfig)
      const updated = await updateJob(editingJob.id, req)
      if (updated) {
        setEditingJob(null)
      } else {
        console.error('Failed to save job')
        showToast('Failed to save job')
      }
    } catch (e) {
      console.error('Failed to save job:', e)
      showToast(e instanceof Error ? e.message : 'Failed to save job')
    } finally {
      setIsSubmittingEdit(false)
    }
  }, [editingJob, showToast, updateJob])

  const handleToggle = useCallback(async () => {
    try {
      if (selectedJob) await toggleJob(selectedJob.id)
    } catch (e) {
      console.error('Failed to toggle job:', e)
      alert('Failed to toggle job')
    }
  }, [selectedJob, toggleJob])

  const handleRunNow = useCallback(async () => {
    try {
      if (selectedJob) await runNow(selectedJob.id)
    } catch (e) {
      console.error('Failed to run job:', e)
      alert('Failed to run job')
    }
  }, [selectedJob, runNow])

  const handleDelete = useCallback(async () => {
    try {
      if (selectedJob) {
        const id = selectedJob.id
        await deleteJob(id)
        selectJob(null)
      }
    } catch (e) {
      console.error('Failed to delete job:', e)
      alert('Failed to delete job')
    }
  }, [selectedJob, deleteJob, selectJob])

  const editingValues = useMemo(
    () => (editingJob ? jobToFormValues(editingJob) : undefined),
    [editingJob],
  )

  return (
    <div className={PAGE_CLS}>
      {toastMessage && (
        <button
          type="button"
          className="app-toast"
          onClick={dismissToast}
          aria-label={`Dismiss notification: ${toastMessage}`}
        >
          {toastMessage}
        </button>
      )}

      <div className={TOOLBAR_CLS}>
        <Heading level={1} className={TOOLBAR_TITLE_CLS}>Cron Jobs</Heading>
        <input
          className={TOOLBAR_SEARCH_CLS}
          type="text"
          placeholder="Search"
          value={filters.search}
          onChange={e => setFilters({ ...filters, search: e.target.value })}
          aria-label="Search jobs"
        />
        <select
          className={TOOLBAR_SELECT_CLS}
          value={filters.enabled === null ? '' : String(filters.enabled)}
          onChange={e => {
            const val = e.target.value
            setFilters({ ...filters, enabled: val === '' ? null : val === 'true' })
          }}
          aria-label="Filter by status"
        >
          <option value="">All Jobs</option>
          <option value="true">Enabled</option>
          <option value="false">Disabled</option>
        </select>
        <button
          type="button"
          className={TOOLBAR_BTN_CLS}
          onClick={refresh}
          disabled={isLoading}
          title="Refresh"
          aria-label="Refresh"
        >
          <RefreshIcon />
        </button>
        <button
          type="button"
          className={cn(BTN_CLS, BTN_PRIMARY_CLS, TOOLBAR_CREATE_CLS)}
          onClick={() => setShowCreateDialog(true)}
        >
          <PlusIcon /> <span>Create</span>
        </button>
      </div>

      <div className={JOB_LIST_CLS}>
        {jobs.length === 0 && !isLoading && (
          <div className={EMPTY_CLS}>
            <CronIcon size={48} />
            <Heading level={3} className={EMPTY_TITLE_CLS}>No cron jobs</Heading>
            <p className={EMPTY_TEXT_CLS}>Create your first scheduled job to get started.</p>
            <button
              type="button"
              className={cn(BTN_CLS, BTN_PRIMARY_CLS)}
              onClick={() => setShowCreateDialog(true)}
            >
              <PlusIcon /> Create Cron Job
            </button>
          </div>
        )}
        {isLoading && jobs.length === 0 && (
          <div className={RUNS_LOADING_EMPTY_CLS}>Loading...</div>
        )}

        {jobs.map(job => {
          const isSelected = selectedJob?.id === job.id
          const dotKey = getStatusDotKey(job)
          return (
            <button
              key={job.id}
              type="button"
              className={cn(
                JOB_ITEM_CLS,
                isSelected && JOB_ITEM_SELECTED_CLS,
                !job.enabled && JOB_ITEM_DISABLED_CLS,
              )}
              onClick={() => selectJob(job)}
            >
              <div className={JOB_ITEM_HEADER_CLS}>
                <span className={cn(JOB_STATUS_DOT_CLS, JOB_STATUS_DOT_BG[dotKey] ?? '')} />
                <span className={JOB_NAME_CLS}>{job.name}</span>
              </div>
              <div className={JOB_ITEM_META_CLS}>
                <span className={cn(ACTION_BADGE_CLS, ACTION_BADGE_BG[job.action_type] ?? '')}>
                  {job.action_type}
                </span>
                <span>{formatSchedule(job)}</span>
              </div>
            </button>
          )
        })}
      </div>

      <SidebarPanel
        isOpen={!!selectedJob}
        onClose={() => selectJob(null)}
        title={selectedJob?.name || 'Job'}
        width={520}
      >
        {selectedJob && (
          <JobDetail
            job={selectedJob}
            runs={runs}
            isRunsLoading={isRunsLoading}
            onToggle={handleToggle}
            onRunNow={handleRunNow}
            onEdit={() => setEditingJob(selectedJob)}
            onDelete={handleDelete}
            onNavigateToPipelineExecution={onNavigateToPipelineExecution}
          />
        )}
      </SidebarPanel>

      <SidebarPanel
        isOpen={showCreateDialog}
        onClose={() => setShowCreateDialog(false)}
        title="Create Cron Job"
        width={480}
      >
        {showCreateDialog && (
          <JobForm
            submitLabel="Create Job"
            isSubmitting={isSubmittingCreate}
            onSubmit={handleCreate}
            onCancel={() => setShowCreateDialog(false)}
          />
        )}
      </SidebarPanel>

      <SidebarPanel
        isOpen={!!editingJob}
        onClose={() => setEditingJob(null)}
        title={editingJob ? `Edit: ${editingJob.name}` : 'Edit Job'}
        width={480}
      >
        {editingJob && editingValues && (
          <JobForm
            key={editingJob.id}
            initialValues={editingValues}
            submitLabel="Save"
            isSubmitting={isSubmittingEdit}
            onSubmit={handleEditSave}
            onCancel={() => setEditingJob(null)}
          />
        )}
      </SidebarPanel>
    </div>
  )
}

function RefreshIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
    </svg>
  )
}

function PlusIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  )
}

function CronIcon({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  )
}
