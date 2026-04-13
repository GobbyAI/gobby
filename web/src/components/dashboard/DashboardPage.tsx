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
import {
  dashboardContentClass,
  dashboardErrorClass,
  dashboardGridClass,
  dashboardLoadingClass,
  dashboardPageClass,
  dashboardToolbarClass,
  dashboardToolbarControlsClass,
  dashboardToolbarUpdatedClass,
} from './dashboardStyles'

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
    <main className={dashboardPageClass}>
      <div className={dashboardToolbarClass}>
        <div className="flex items-center gap-3">
          <h2 className="m-0 text-lg font-semibold text-foreground">Dashboard</h2>
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between md:justify-end">
          <div className={dashboardToolbarControlsClass}>
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
            <span className={dashboardToolbarUpdatedClass}>
              Updated {formatTime(lastUpdated)}
            </span>
          )}
        </div>
      </div>

      <div className={dashboardContentClass}>
        {isLoading && !data ? (
          <div className={dashboardLoadingClass}>Loading dashboard...</div>
        ) : error && !data ? (
          <div className={dashboardErrorClass}>Failed to load: {error.slice(0, 200)}</div>
        ) : data ? (
          <div className={dashboardGridClass}>
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
