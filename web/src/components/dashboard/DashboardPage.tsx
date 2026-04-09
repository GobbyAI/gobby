import { useState } from 'react'
import { useDashboard } from '../../hooks/useDashboard'
import { cn } from '../../lib/utils'
import { SystemHealthCard } from './SystemHealthCard'
import { TasksCard } from './TasksCard'
import { SessionsCard } from './SessionsCard'
import { MemoryCard } from './MemoryCard'
import { SavingsCard } from './SavingsCard'
import { UsageCard } from './UsageCard'
import { MetricsChartsCard } from './MetricsChartsCard'
import { TokenEfficiencyCard } from './TokenEfficiencyCard'
import { TimeRangePills } from './TimeRangePills'
import { rangeToHours, type TimeRange } from './timeRangeUtils'
import './DashboardPage.css'

function formatTime(date: Date | null): string {
  if (!date) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function DashboardPage() {
  const { data, isLoading, error, lastUpdated } = useDashboard()
  const [timeRange, setTimeRange] = useState<TimeRange>('all')
  const [showAllProjects, setShowAllProjects] = useState(true)

  const hours = rangeToHours(timeRange)
  const projectId = showAllProjects ? undefined : data?.project_id

  return (
    <main className="dash-page">
      <div className="dash-toolbar">
        <div className="dash-toolbar-left">
          <h2 className="dash-toolbar-title">Dashboard</h2>
        </div>
        <div className="dash-toolbar-right">
          <div className="flex items-center gap-3">
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none">
              <button
                role="switch"
                aria-checked={showAllProjects}
                onClick={() => setShowAllProjects(!showAllProjects)}
                className={cn(
                  'relative inline-flex h-4 w-7 shrink-0 rounded-full border border-border transition-colors',
                  showAllProjects ? 'bg-accent' : 'bg-muted',
                )}
              >
                <span
                  className={cn(
                    'pointer-events-none block h-3 w-3 rounded-full bg-white shadow-sm transition-transform',
                    showAllProjects ? 'translate-x-3' : 'translate-x-0',
                  )}
                />
              </button>
              All Projects
            </label>
            <TimeRangePills value={timeRange} onChange={setTimeRange} />
          </div>
          {lastUpdated && (
            <span className="dash-toolbar-updated">
              Updated {formatTime(lastUpdated)}
            </span>
          )}
        </div>
      </div>

      <div className="dash-content">
        {isLoading && !data ? (
          <div className="dash-loading">Loading dashboard...</div>
        ) : error && !data ? (
          <div className="dash-error">Failed to load: {error.slice(0, 200)}</div>
        ) : data ? (
          <div className="dash-grid">
            {/* Row 1: System + donut cards */}
            <SystemHealthCard data={data} />
            <TasksCard hours={hours} projectId={projectId} />
            <SessionsCard hours={hours} projectId={projectId} />

            {/* Row 2: Usage + Savings + Memory */}
            <UsageCard hours={hours} projectId={projectId} />
            <SavingsCard hours={hours} projectId={projectId} />
            <MemoryCard hours={hours} projectId={projectId} />

            {/* Row 3: Token efficiency chart (full width) */}
            <TokenEfficiencyCard hours={hours} projectId={projectId} />

            {/* Row 4: System metrics charts (full width) */}
            <MetricsChartsCard hours={hours} />
          </div>
        ) : null}
      </div>
    </main>
  )
}
