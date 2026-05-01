import { useState, useCallback, useMemo } from 'react'
import { useCronJobs } from '../hooks/useCronJobs'
import type { CronJob, CronRun, CreateCronJobRequest, UpdateCronJobRequest } from '../hooks/useCronJobs'
import { SidebarPanel } from './shared/SidebarPanel'
import './CronJobsPage.css'

// =============================================================================
// Types
// =============================================================================

type ScheduleType = CronJob['schedule_type']
type ActionType = CronJob['action_type']

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

// =============================================================================
// Helpers
// =============================================================================

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

function formatRelativeTime(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime()
  if (diff < 0) return 'in the future'
  const mins = Math.floor(diff / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function formatDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt || !completedAt) return '-'
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime()
  if (ms < 1000) return `${ms}ms`
  const secs = Math.floor(ms / 1000)
  if (secs < 60) return `${secs}s`
  const mins = Math.floor(secs / 60)
  return `${mins}m ${secs % 60}s`
}

function getStatusDotClass(job: CronJob): string {
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

function formValuesToCreateRequest(v: JobFormValues): CreateCronJobRequest {
  const actionConfig = JSON.parse(v.actionConfigStr) as Record<string, unknown>
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

function formValuesToUpdateRequest(v: JobFormValues): UpdateCronJobRequest {
  const actionConfig = JSON.parse(v.actionConfigStr) as Record<string, unknown>
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

// =============================================================================
// Job Form (shared by create + edit drawers)
// =============================================================================

interface JobFormProps {
  initialValues?: JobFormValues
  submitLabel: string
  isSubmitting?: boolean
  onSubmit: (values: JobFormValues) => void | Promise<void>
  onCancel: () => void
}

function JobForm({ initialValues, submitLabel, isSubmitting, onSubmit, onCancel }: JobFormProps) {
  const [values, setValues] = useState<JobFormValues>(initialValues || DEFAULT_FORM_VALUES)

  const isFormValid = useMemo(() => {
    if (!values.name.trim()) return false
    try { JSON.parse(values.actionConfigStr) } catch { return false }
    if (values.scheduleType === 'cron' && !values.cronExpr.trim()) return false
    if (values.scheduleType === 'interval') {
      const parsed = parseInt(values.intervalSeconds, 10)
      if (isNaN(parsed) || parsed < 10) return false
    }
    return true
  }, [values])

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
    if (!isFormValid || isSubmitting) return
    onSubmit(values)
  }

  return (
    <div className="cron-form">
      <div className="cron-form-body">
        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-name">Name</label>
          <input
            id="cron-form-name"
            className="cron-form-input"
            value={values.name}
            onChange={e => update('name', e.target.value)}
            placeholder="My Scheduled Job"
            autoFocus
          />
        </div>

        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-description">Description</label>
          <input
            id="cron-form-description"
            className="cron-form-input"
            value={values.description}
            onChange={e => update('description', e.target.value)}
            placeholder="Optional description"
          />
        </div>

        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-schedule-type">Schedule Type</label>
          <select
            id="cron-form-schedule-type"
            className="cron-form-select"
            value={values.scheduleType}
            onChange={e => update('scheduleType', e.target.value as ScheduleType)}
          >
            <option value="cron">Cron Expression</option>
            <option value="interval">Fixed Interval</option>
            <option value="once">One-shot</option>
          </select>
        </div>

        {values.scheduleType === 'cron' && (
          <div className="cron-form-group">
            <label className="cron-form-label" htmlFor="cron-form-cron-expr">Cron Expression</label>
            <input
              id="cron-form-cron-expr"
              className="cron-form-input"
              value={values.cronExpr}
              onChange={e => update('cronExpr', e.target.value)}
              placeholder="0 7 * * *"
            />
          </div>
        )}

        {values.scheduleType === 'interval' && (
          <div className="cron-form-group">
            <label className="cron-form-label" htmlFor="cron-form-interval">Interval (seconds)</label>
            <input
              id="cron-form-interval"
              className="cron-form-input"
              type="number"
              value={values.intervalSeconds}
              onChange={e => update('intervalSeconds', e.target.value)}
              min="10"
            />
          </div>
        )}

        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-timezone">Timezone</label>
          <input
            id="cron-form-timezone"
            className="cron-form-input"
            value={values.timezone}
            onChange={e => update('timezone', e.target.value)}
            placeholder="UTC"
          />
        </div>

        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-action-type">Action Type</label>
          <select
            id="cron-form-action-type"
            className="cron-form-select"
            value={values.actionType}
            onChange={e => handleActionTypeChange(e.target.value as ActionType)}
          >
            <option value="shell">Shell Command</option>
            <option value="agent_spawn">Agent Spawn</option>
            <option value="pipeline">Pipeline</option>
          </select>
        </div>

        <div className="cron-form-group">
          <label className="cron-form-label" htmlFor="cron-form-action-config">Action Config (JSON)</label>
          <textarea
            id="cron-form-action-config"
            className="cron-form-textarea cron-form-json"
            value={values.actionConfigStr}
            onChange={e => update('actionConfigStr', e.target.value)}
            rows={6}
          />
        </div>
      </div>

      <div className="cron-form-actions">
        <button type="button" className="cron-btn" onClick={onCancel}>Cancel</button>
        <button
          type="button"
          className="cron-btn primary"
          onClick={handleSubmit}
          disabled={!isFormValid || isSubmitting}
        >
          {isSubmitting ? 'Saving...' : submitLabel}
        </button>
      </div>
    </div>
  )
}

// =============================================================================
// Run History Table
// =============================================================================

function RunHistoryTable({ runs, isLoading }: { runs: CronRun[]; isLoading: boolean }) {
  if (isLoading) {
    return <div className="cron-runs-empty">Loading runs...</div>
  }
  if (runs.length === 0) {
    return <div className="cron-runs-empty">No runs yet</div>
  }

  return (
    <div className="cron-runs-table-scroll">
      <table className="cron-runs-table">
        <thead>
          <tr>
            <th>Triggered</th>
            <th>Status</th>
            <th>Duration</th>
            <th>Output</th>
          </tr>
        </thead>
        <tbody>
          {runs.map(run => (
            <tr key={run.id}>
              <td title={run.triggered_at}>{formatRelativeTime(run.triggered_at)}</td>
              <td>
                <span className={`cron-run-status ${run.status}`}>
                  {run.status}
                </span>
              </td>
              <td>{formatDuration(run.started_at, run.completed_at)}</td>
              <td className="cron-runs-table-output">
                {run.error || run.output || '-'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// =============================================================================
// Job Detail (rendered inside SidebarPanel)
// =============================================================================

interface JobDetailProps {
  job: CronJob
  runs: CronRun[]
  isRunsLoading: boolean
  onToggle: () => void
  onRunNow: () => void
  onEdit: () => void
  onDelete: () => void
}

function JobDetail({ job, runs, isRunsLoading, onToggle, onRunNow, onEdit, onDelete }: JobDetailProps) {
  const [confirmDelete, setConfirmDelete] = useState(false)

  return (
    <div className="cron-detail">
      <div className="cron-detail-header">
        <div className="cron-detail-title-block">
          {job.description && <p className="cron-detail-description">{job.description}</p>}
        </div>
        <div className="cron-detail-actions">
          <button className="cron-btn primary" onClick={onRunNow}>Run Now</button>
          <button className="cron-btn" onClick={onEdit}>Edit</button>
          <button className="cron-btn" onClick={onToggle}>
            {job.enabled ? 'Disable' : 'Enable'}
          </button>
          {confirmDelete ? (
            <>
              <button className="cron-btn danger" onClick={onDelete}>Confirm</button>
              <button className="cron-btn" onClick={() => setConfirmDelete(false)}>Cancel</button>
            </>
          ) : (
            <button className="cron-btn danger" onClick={() => setConfirmDelete(true)}>Delete</button>
          )}
        </div>
      </div>

      <div className="cron-info-grid">
        <div className="cron-info-card">
          <div className="cron-info-label">Schedule</div>
          <div className="cron-info-value">
            <code>{formatSchedule(job)}</code>
          </div>
        </div>
        <div className="cron-info-card">
          <div className="cron-info-label">Timezone</div>
          <div className="cron-info-value">{job.timezone}</div>
        </div>
        <div className="cron-info-card">
          <div className="cron-info-label">Status</div>
          <div className="cron-info-value">
            {job.enabled ? 'Active' : 'Disabled'}
            {job.consecutive_failures > 0 && ` (${job.consecutive_failures} failures)`}
          </div>
        </div>
        <div className="cron-info-card">
          <div className="cron-info-label">Action Type</div>
          <div className="cron-info-value">
            <span className={`cron-action-badge ${job.action_type}`}>{job.action_type}</span>
          </div>
        </div>
        <div className="cron-info-card">
          <div className="cron-info-label">Next Run</div>
          <div className="cron-info-value">
            {job.next_run_at ? new Date(job.next_run_at).toLocaleString() : '-'}
          </div>
        </div>
        <div className="cron-info-card">
          <div className="cron-info-label">Last Run</div>
          <div className="cron-info-value">
            {job.last_run_at ? formatRelativeTime(job.last_run_at) : 'Never'}
            {job.last_status && ` (${job.last_status})`}
          </div>
        </div>
      </div>

      <div className="cron-config-section">
        <h4>Action Config</h4>
        <pre className="cron-config-pre">
          {JSON.stringify(job.action_config, null, 2)}
        </pre>
      </div>

      <div className="cron-runs-section">
        <h4>Recent Runs</h4>
        <RunHistoryTable runs={runs} isLoading={isRunsLoading} />
      </div>
    </div>
  )
}

// =============================================================================
// Main Page Component
// =============================================================================

export function CronJobsPage({ projectId }: { projectId?: string | null }) {
  const {
    jobs, selectedJob, selectJob, runs, filters, setFilters,
    isLoading, isRunsLoading, createJob, updateJob, deleteJob, toggleJob, runNow, refresh,
  } = useCronJobs(projectId)

  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [editingJob, setEditingJob] = useState<CronJob | null>(null)
  const [isSubmittingCreate, setIsSubmittingCreate] = useState(false)
  const [isSubmittingEdit, setIsSubmittingEdit] = useState(false)

  const handleCreate = useCallback(async (values: JobFormValues) => {
    setIsSubmittingCreate(true)
    try {
      const req = formValuesToCreateRequest(values)
      const job = await createJob(req)
      if (job) {
        setShowCreateDialog(false)
        selectJob(job)
      }
    } catch (e) {
      console.error('Failed to create job:', e)
      alert('Failed to create job')
    } finally {
      setIsSubmittingCreate(false)
    }
  }, [createJob, selectJob])

  const handleEditSave = useCallback(async (values: JobFormValues) => {
    if (!editingJob) return
    setIsSubmittingEdit(true)
    try {
      const req = formValuesToUpdateRequest(values)
      const updated = await updateJob(editingJob.id, req)
      if (updated) {
        setEditingJob(null)
      } else {
        alert('Failed to save job')
      }
    } catch (e) {
      console.error('Failed to save job:', e)
      alert('Failed to save job')
    } finally {
      setIsSubmittingEdit(false)
    }
  }, [editingJob, updateJob])

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
    <div className="cron-page">
      <div className="cron-toolbar">
        <h2 className="cron-toolbar-title">Cron Jobs</h2>
        <input
          className="cron-toolbar-search"
          type="text"
          placeholder="Search jobs..."
          value={filters.search}
          onChange={e => setFilters({ ...filters, search: e.target.value })}
          aria-label="Search jobs"
        />
        <select
          className="cron-toolbar-select"
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
          className="cron-toolbar-btn"
          onClick={refresh}
          disabled={isLoading}
          title="Refresh"
          aria-label="Refresh"
        >
          <RefreshIcon />
        </button>
        <button
          type="button"
          className="cron-btn primary cron-toolbar-create"
          onClick={() => setShowCreateDialog(true)}
        >
          <PlusIcon /> <span>Create</span>
        </button>
      </div>

      <div className="cron-job-list">
        {jobs.length === 0 && !isLoading && (
          <div className="cron-empty">
            <CronIcon size={48} />
            <h3>No cron jobs</h3>
            <p>Create your first scheduled job to get started.</p>
            <button
              type="button"
              className="cron-btn primary"
              onClick={() => setShowCreateDialog(true)}
            >
              <PlusIcon /> Create Cron Job
            </button>
          </div>
        )}
        {isLoading && jobs.length === 0 && (
          <div className="cron-runs-empty">Loading...</div>
        )}

        {jobs.map(job => (
          <button
            key={job.id}
            type="button"
            className={`cron-job-item ${selectedJob?.id === job.id ? 'selected' : ''} ${!job.enabled ? 'disabled' : ''}`}
            onClick={() => selectJob(job)}
          >
            <div className="cron-job-item-header">
              <span className={`cron-job-status-dot ${getStatusDotClass(job)}`} />
              <span className="cron-job-name">{job.name}</span>
            </div>
            <div className="cron-job-item-meta">
              <span className={`cron-action-badge ${job.action_type}`}>{job.action_type}</span>
              <span>{formatSchedule(job)}</span>
            </div>
          </button>
        ))}
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

// =============================================================================
// Icons
// =============================================================================

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
