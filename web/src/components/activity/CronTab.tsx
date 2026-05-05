import { memo, useEffect, useMemo, useState } from 'react'
import { ResizeHandle } from '../chat/artifacts/ResizeHandle'
import { SegmentedControl } from '../ui/SegmentedControl'
import { formatDateTime } from '../workflows/executionFormatters'
import { useCronJobs } from '../../hooks/useCronJobs'
import type { CronJob, CronRun } from '../../hooks/useCronJobs'
import { cronRunStatusKind } from './cronRunStatus'
import { ActivityPanelEmpty, CronEmptyIcon } from './ActivityPanelEmpty'

interface CronTabProps {
  projectId?: string | null
}

const FILTER_OPTIONS = [
  { id: 'all', label: 'All' },
  { id: 'enabled', label: 'Enabled' },
  { id: 'disabled', label: 'Disabled' },
] as const

type StatusFilter = (typeof FILTER_OPTIONS)[number]['id']

const PAGE_SIZE = 20

export const CronTab = memo(function CronTab({ projectId }: CronTabProps) {
  const { jobs, selectedJob, selectJob, runs, isRunsLoading, isLoading } = useCronJobs(projectId)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [topHeight, setTopHeight] = useState(50)
  const [displayLimitState, setDisplayLimitState] = useState<{
    filter: StatusFilter
    limit: number
  }>({ filter: 'all', limit: PAGE_SIZE })
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    let interval: number | null = null
    const stopTimer = () => {
      if (interval !== null) {
        window.clearInterval(interval)
        interval = null
      }
    }
    const startTimer = () => {
      stopTimer()
      setNow(Date.now())
      interval = window.setInterval(() => setNow(Date.now()), 15_000)
    }
    const syncTimer = () => {
      if (document.visibilityState === 'visible') {
        startTimer()
      } else {
        stopTimer()
      }
    }

    syncTimer()
    document.addEventListener('visibilitychange', syncTimer)
    return () => {
      stopTimer()
      document.removeEventListener('visibilitychange', syncTimer)
    }
  }, [])

  const filteredJobs = useMemo(() => {
    if (statusFilter === 'all') return jobs
    if (statusFilter === 'enabled') return jobs.filter((j) => j.enabled)
    return jobs.filter((j) => !j.enabled)
  }, [jobs, statusFilter])

  const displayLimit =
    displayLimitState.filter === statusFilter ? displayLimitState.limit : PAGE_SIZE

  const visibleJobs = useMemo(
    () => filteredJobs.slice(0, displayLimit),
    [filteredJobs, displayLimit],
  )
  const hasMore = filteredJobs.length > visibleJobs.length

  // If the selected job is filtered out, clear the selection so the detail
  // pane doesn't refer to an invisible row.
  useEffect(() => {
    if (selectedJob && !filteredJobs.some((j) => j.id === selectedJob.id)) {
      selectJob(null)
    }
  }, [filteredJobs, selectedJob, selectJob])

  if (isLoading && jobs.length === 0) {
    return <ActivityPanelEmpty body="Loading cron jobs…" />
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border">
        <SegmentedControl<StatusFilter>
          value={statusFilter}
          onChange={setStatusFilter}
          options={FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label }))}
          ariaLabel="Cron status filter"
        />
      </div>

      <div
        className={`overflow-y-auto ${selectedJob ? 'border-b border-border' : 'flex-1'}`}
        style={selectedJob ? { height: `${topHeight}%` } : undefined}
      >
        {filteredJobs.length === 0 ? (
          <ActivityPanelEmpty
            icon={<CronEmptyIcon />}
            heading="Cron Jobs"
            body={
              statusFilter === 'all'
                ? 'Cron jobs appear here when scheduled'
                : `No ${statusFilter} cron jobs yet`
            }
          />
        ) : (
          <>
            {visibleJobs.map((job) => (
              <button
                key={job.id}
                type="button"
                className={`pipeline-exec-row${selectedJob?.id === job.id ? ' pipeline-exec-row--active' : ''}`}
                onClick={() => selectJob(job)}
              >
                <div className="flex items-center gap-2 min-w-0">
                  <CronStatusDot enabled={job.enabled} />
                  <span className="activity-row-title">{job.name}</span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="activity-row-meta">
                    {formatNextFiring(job, now)}
                  </span>
                </div>
              </button>
            ))}
            {hasMore && (
              <button
                type="button"
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors pointer-coarse:min-h-11"
                onClick={() =>
                  setDisplayLimitState({
                    filter: statusFilter,
                    limit: displayLimit + PAGE_SIZE,
                  })
                }
              >
                Load more
              </button>
            )}
          </>
        )}
      </div>

      {selectedJob && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {selectedJob && (
        <div className="flex-1 flex flex-col min-h-0">
          <div className="pipeline-detail-header flex items-center gap-3 px-3 border-b border-border">
            <div className="flex items-center gap-2 min-w-0">
              <CronStatusDot enabled={selectedJob.enabled} />
              <span className="activity-row-title">
                {selectedJob.name}
              </span>
              <span className="activity-row-meta">
                {selectedJob.schedule_type === 'cron'
                  ? selectedJob.cron_expr
                  : selectedJob.schedule_type === 'interval'
                    ? `every ${selectedJob.interval_seconds ?? '?'}s`
                    : selectedJob.schedule_type === 'once'
                      ? 'once'
                      : ''}
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {isRunsLoading && runs.length === 0 ? (
              <p className="text-xs text-muted-foreground p-2">Loading runs...</p>
            ) : runs.length === 0 ? (
              <p className="text-xs text-muted-foreground p-2">No runs yet</p>
            ) : (
              <ul className="cron-tab-runs">
                {runs.map((run) => (
                  <li key={run.id} className="cron-tab-run">
                    <RunStatusGlyph status={run.status} />
                    <span className="cron-tab-run__time">
                      {formatDateTime(run.triggered_at)}
                    </span>
                    <span className="cron-tab-run__status">{run.status}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  )
})

function CronStatusDot({ enabled }: { enabled: boolean }) {
  if (enabled) {
    return <span className="pipeline-running-dot" aria-label="Enabled" />
  }
  return (
    <span
      className="inline-block w-2 h-2 rounded-full bg-muted-foreground/40 shrink-0"
      aria-label="Disabled"
    />
  )
}

function RunStatusGlyph({ status }: { status: string }) {
  const kind = cronRunStatusKind(status)
  if (kind === 'success') {
    return <span className="text-success-foreground text-xs shrink-0">{'✓'}</span>
  }
  if (kind === 'failure') {
    return <span className="text-error text-xs shrink-0">{'✗'}</span>
  }
  if (kind === 'running') {
    return <span className="pipeline-running-dot" />
  }
  return <span className="text-muted-foreground text-xs shrink-0">{'○'}</span>
}

function formatNextFiring(job: CronJob, now: number): string {
  if (!job.enabled) return 'disabled'
  if (!job.next_run_at) return '—'
  const ms = new Date(job.next_run_at).getTime() - now
  if (ms <= 0) return 'due'
  const sec = Math.floor(ms / 1000)
  if (sec < 60) return 'in <1m'
  const min = Math.floor(sec / 60)
  if (min < 60) return `in ${min}m`
  const hr = Math.floor(min / 60)
  if (hr < 24) {
    const remMin = min % 60
    return remMin > 0 ? `in ${hr}h ${remMin}m` : `in ${hr}h`
  }
  const day = Math.floor(hr / 24)
  return `in ${day}d`
}

export type { CronJob, CronRun }
