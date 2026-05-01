import { memo, useEffect, useMemo, useState } from 'react'
import { useTraces } from '../../hooks/useTraces'
import { useCronJobs } from '../../hooks/useCronJobs'
import type { TraceRecord } from '../../hooks/useTraces'
import type { CronJob } from '../../hooks/useCronJobs'

const RECENT_TRACES_LIMIT = 5
const NEXT_CRON_LIMIT = 5

interface OverviewTabProps {
  projectId?: string | null
  onNavigateToPage?: (tab: string) => void
  onNavigateToTrace?: (traceId: string) => void
}

export const OverviewTab = memo(function OverviewTab({
  projectId,
  onNavigateToPage,
  onNavigateToTrace,
}: OverviewTabProps) {
  const { traces, isLoading: tracesLoading } = useTraces(projectId ?? undefined)
  const { jobs, isLoading: jobsLoading } = useCronJobs(projectId)

  const recentTraces = useMemo(() => {
    return [...traces]
      .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
      .slice(0, RECENT_TRACES_LIMIT)
  }, [traces])

  const nextFirings = useMemo(() => {
    return jobs
      .filter((j) => j.enabled && j.next_run_at)
      .sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())
      .slice(0, NEXT_CRON_LIMIT)
  }, [jobs])

  const handleTraceClick = (traceId: string) => {
    if (onNavigateToTrace) {
      onNavigateToTrace(traceId)
    } else if (onNavigateToPage) {
      onNavigateToPage('traces')
    }
  }

  const handleViewAllTraces = () => {
    if (onNavigateToPage) onNavigateToPage('traces')
  }

  const handleViewAllCron = () => {
    if (onNavigateToPage) onNavigateToPage('cron')
  }

  const handleCronClick = () => {
    if (onNavigateToPage) onNavigateToPage('cron')
  }

  return (
    <div className="overview-tab">
      <RecentTracesWidget
        traces={recentTraces}
        isLoading={tracesLoading}
        onTraceClick={handleTraceClick}
        onViewAll={handleViewAllTraces}
      />
      <NextFiringsWidget
        firings={nextFirings}
        isLoading={jobsLoading}
        onJobClick={handleCronClick}
        onViewAll={handleViewAllCron}
      />
    </div>
  )
})

interface RecentTracesWidgetProps {
  traces: TraceRecord[]
  isLoading: boolean
  onTraceClick: (traceId: string) => void
  onViewAll: () => void
}

function RecentTracesWidget({ traces, isLoading, onTraceClick, onViewAll }: RecentTracesWidgetProps) {
  return (
    <section className="overview-widget" aria-labelledby="overview-traces-heading">
      <header className="overview-widget__header">
        <h3 id="overview-traces-heading" className="overview-widget__title">
          Recent traces
        </h3>
        <button
          type="button"
          className="overview-widget__view-all"
          onClick={onViewAll}
          title="Open Traces page"
        >
          View all <span aria-hidden="true">{'→'}</span>
        </button>
      </header>
      {isLoading && traces.length === 0 ? (
        <p className="overview-widget__empty">Loading traces...</p>
      ) : traces.length === 0 ? (
        <p className="overview-widget__empty">No traces yet</p>
      ) : (
        <ul className="overview-widget__list">
          {traces.map((trace) => (
            <li key={trace.trace_id}>
              <button
                type="button"
                className="overview-trace-row"
                onClick={() => onTraceClick(trace.trace_id)}
              >
                <span
                  className={`overview-trace-status overview-trace-status--${trace.status.toLowerCase()}`}
                  title={trace.status}
                  aria-label={`Status: ${trace.status}`}
                />
                <span className="overview-trace-name" title={trace.root_span_name || trace.trace_id}>
                  {trace.root_span_name || 'Unknown span'}
                </span>
                <span className="overview-trace-duration">
                  {formatDurationMs(trace.duration_ms)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

interface NextFiringsWidgetProps {
  firings: CronJob[]
  isLoading: boolean
  onJobClick: (jobId: string) => void
  onViewAll: () => void
}

function NextFiringsWidget({ firings, isLoading, onJobClick, onViewAll }: NextFiringsWidgetProps) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(interval)
  }, [])

  return (
    <section className="overview-widget" aria-labelledby="overview-cron-heading">
      <header className="overview-widget__header">
        <h3 id="overview-cron-heading" className="overview-widget__title">
          Next cron firings
        </h3>
        <button
          type="button"
          className="overview-widget__view-all"
          onClick={onViewAll}
          title="Open Cron page"
        >
          View all <span aria-hidden="true">{'→'}</span>
        </button>
      </header>
      {isLoading && firings.length === 0 ? (
        <p className="overview-widget__empty">Loading jobs...</p>
      ) : firings.length === 0 ? (
        <p className="overview-widget__empty">No upcoming firings</p>
      ) : (
        <ul className="overview-widget__list">
          {firings.map((job) => {
            const nextMs = new Date(job.next_run_at!).getTime()
            const deltaMs = nextMs - now
            return (
              <li key={job.id}>
                <button
                  type="button"
                  className="overview-cron-row"
                  onClick={() => onJobClick(job.id)}
                >
                  <span className="overview-cron-name" title={job.name}>
                    {job.name}
                  </span>
                  <span
                    className={`overview-cron-countdown${deltaMs <= 0 ? ' overview-cron-countdown--due' : ''}`}
                  >
                    {formatCountdown(deltaMs)}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

function formatDurationMs(ms: number): string {
  if (ms < 1) return '<1ms'
  if (ms < 1000) return `${ms.toFixed(0)}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`
  return `${(ms / 60_000).toFixed(1)}m`
}

function formatCountdown(deltaMs: number): string {
  if (deltaMs <= 0) return 'due'
  const totalSec = Math.floor(deltaMs / 1000)
  if (totalSec < 60) return `in ${totalSec}s`
  const totalMin = Math.floor(totalSec / 60)
  if (totalMin < 60) return `in ${totalMin}m`
  const totalHr = Math.floor(totalMin / 60)
  if (totalHr < 24) {
    const remMin = totalMin % 60
    return remMin > 0 ? `in ${totalHr}h ${remMin}m` : `in ${totalHr}h`
  }
  const totalDay = Math.floor(totalHr / 24)
  const remHr = totalHr % 24
  return remHr > 0 ? `in ${totalDay}d ${remHr}h` : `in ${totalDay}d`
}
