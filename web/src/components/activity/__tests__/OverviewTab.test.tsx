import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

import { OverviewTab } from '../OverviewTab'
import type { TraceRecord } from '../../../hooks/useTraces'
import type { CronJob } from '../../../hooks/useCronJobs'

const tracesMock = vi.hoisted(() => ({
  traces: [] as TraceRecord[],
  isLoading: false,
}))
const cronMock = vi.hoisted(() => ({
  jobs: [] as CronJob[],
  isLoading: false,
}))

vi.mock('../../../hooks/useTraces', () => ({
  useTraces: () => tracesMock,
}))
vi.mock('../../../hooks/useCronJobs', () => ({
  useCronJobs: () => cronMock,
}))

function makeTrace(overrides: Partial<TraceRecord> = {}): TraceRecord {
  return {
    id: 'row-1',
    project_id: 'proj-1',
    trace_id: 'trace-aaaaaaaaaaaa',
    root_span_name: 'GET /api/foo',
    status: 'OK',
    start_time_ns: 0,
    end_time_ns: 0,
    duration_ms: 12.34,
    timestamp: '2026-05-01T12:00:00Z',
    ...overrides,
  }
}

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'job-1',
    project_id: 'proj-1',
    name: 'Nightly cleanup',
    description: null,
    schedule_type: 'cron',
    cron_expr: '0 3 * * *',
    interval_seconds: null,
    run_at: null,
    timezone: 'UTC',
    action_type: 'shell',
    action_config: {},
    enabled: true,
    next_run_at: '2099-01-01T00:00:00Z',
    last_run_at: null,
    last_status: null,
    consecutive_failures: 0,
    created_at: '2026-05-01T00:00:00Z',
    updated_at: '2026-05-01T00:00:00Z',
    ...overrides,
  }
}

beforeEach(() => {
  tracesMock.traces = []
  tracesMock.isLoading = false
  cronMock.jobs = []
  cronMock.isLoading = false
})

afterEach(() => {
  vi.useRealTimers()
})

describe('OverviewTab', () => {
  it('renders empty states when there are no traces or cron firings', () => {
    render(<OverviewTab projectId="proj-1" />)
    expect(screen.getByText(/no traces yet/i)).toBeInTheDocument()
    expect(screen.getByText(/no upcoming firings/i)).toBeInTheDocument()
  })

  it('shows the most recent traces sorted newest-first and capped at 5', () => {
    tracesMock.traces = [
      makeTrace({ trace_id: 't-old', root_span_name: 'old-span', timestamp: '2026-04-01T00:00:00Z' }),
      makeTrace({ trace_id: 't-new', root_span_name: 'new-span', timestamp: '2026-05-01T12:00:00Z' }),
      makeTrace({ trace_id: 't-mid', root_span_name: 'mid-span', timestamp: '2026-04-15T00:00:00Z' }),
      makeTrace({ trace_id: 't-4', root_span_name: 's4', timestamp: '2026-04-02T00:00:00Z' }),
      makeTrace({ trace_id: 't-5', root_span_name: 's5', timestamp: '2026-04-03T00:00:00Z' }),
      makeTrace({ trace_id: 't-6', root_span_name: 's6-overflow', timestamp: '2026-03-01T00:00:00Z' }),
    ]

    render(<OverviewTab projectId="proj-1" />)
    const newest = screen.getAllByRole('button', { name: /span/i })
    expect(newest[0]).toHaveTextContent('new-span')
    expect(screen.queryByText('s6-overflow')).toBeNull()
  })

  it('lists only enabled jobs with a next_run_at, sorted ascending and capped at 5', () => {
    cronMock.jobs = [
      makeJob({ id: 'd', name: 'disabled', enabled: false }),
      makeJob({ id: 'no-next', name: 'no-next', next_run_at: null }),
      makeJob({ id: 'late', name: 'late', next_run_at: '2099-12-01T00:00:00Z' }),
      makeJob({ id: 'soon', name: 'soon', next_run_at: '2099-01-01T00:00:00Z' }),
    ]

    render(<OverviewTab projectId="proj-1" />)
    expect(screen.queryByText('disabled')).toBeNull()
    expect(screen.queryByText('no-next')).toBeNull()
    const rows = screen.getAllByRole('button').filter((b) => b.className.includes('overview-cron-row'))
    expect(rows[0]).toHaveTextContent('soon')
    expect(rows[1]).toHaveTextContent('late')
  })

  it('clicking a trace row calls onNavigateToTrace with the trace id', async () => {
    tracesMock.traces = [makeTrace({ trace_id: 'trace-xyz', root_span_name: 'click-me' })]
    const onNavigateToTrace = vi.fn()
    const onNavigateToPage = vi.fn()
    render(
      <OverviewTab
        projectId="proj-1"
        onNavigateToTrace={onNavigateToTrace}
        onNavigateToPage={onNavigateToPage}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /click-me/i }))
    expect(onNavigateToTrace).toHaveBeenCalledWith('trace-xyz')
  })

  it('clicking "View all" on traces navigates to the traces page', async () => {
    const onNavigateToPage = vi.fn()
    render(<OverviewTab projectId="proj-1" onNavigateToPage={onNavigateToPage} />)
    const buttons = screen.getAllByRole('button', { name: /view all/i })
    await userEvent.click(buttons[0])
    expect(onNavigateToPage).toHaveBeenCalledWith('traces')
  })

  it('clicking "View all" on cron firings navigates to the cron page', async () => {
    const onNavigateToPage = vi.fn()
    render(<OverviewTab projectId="proj-1" onNavigateToPage={onNavigateToPage} />)
    const buttons = screen.getAllByRole('button', { name: /view all/i })
    await userEvent.click(buttons[1])
    expect(onNavigateToPage).toHaveBeenCalledWith('cron')
  })

  it('shows "due" when the next run is in the past', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-01T12:00:00Z'))
    cronMock.jobs = [
      makeJob({ id: 'past', name: 'late-job', next_run_at: '2026-04-01T00:00:00Z' }),
    ]
    render(<OverviewTab projectId="proj-1" />)
    expect(screen.getByText(/^due$/i)).toBeInTheDocument()
  })

  it('formats the countdown for an upcoming firing', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-05-01T12:00:00Z'))
    cronMock.jobs = [
      makeJob({ id: 'future', name: 'soon-job', next_run_at: '2026-05-01T12:05:00Z' }),
    ]
    render(<OverviewTab projectId="proj-1" />)
    expect(screen.getByText(/in 5m/i)).toBeInTheDocument()
  })
})
