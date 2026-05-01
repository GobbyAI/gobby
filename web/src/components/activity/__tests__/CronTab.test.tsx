import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { CronTab } from '../CronTab'
import type { CronJob, CronRun } from '../../../hooks/useCronJobs'

const cronMock = vi.hoisted(() => ({
  jobs: [] as CronJob[],
  selectedJob: null as CronJob | null,
  selectJob: vi.fn(),
  runs: [] as CronRun[],
  isRunsLoading: false,
  isLoading: false,
}))

vi.mock('../../../hooks/useCronJobs', () => ({
  useCronJobs: () => cronMock,
}))

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => null,
}))

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'job-1',
    project_id: 'p',
    name: 'job-name',
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
  cronMock.jobs = []
  cronMock.selectedJob = null
  cronMock.selectJob = vi.fn()
  cronMock.runs = []
  cronMock.isRunsLoading = false
  cronMock.isLoading = false
})

describe('CronTab', () => {
  it('renders an empty state when no jobs are loaded', () => {
    render(<CronTab projectId="p" />)
    expect(screen.getByText(/no cron jobs/i)).toBeInTheDocument()
  })

  it('renders rows for each job and calls selectJob on click', async () => {
    cronMock.jobs = [makeJob({ id: 'a', name: 'alpha' }), makeJob({ id: 'b', name: 'beta' })]
    render(<CronTab projectId="p" />)
    const alpha = screen.getByRole('button', { name: /alpha/i })
    expect(alpha).toBeInTheDocument()
    await userEvent.click(alpha)
    expect(cronMock.selectJob).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }))
  })

  it('filters by enabled / disabled', async () => {
    cronMock.jobs = [
      makeJob({ id: 'on', name: 'live-job', enabled: true }),
      makeJob({ id: 'off', name: 'paused-job', enabled: false }),
    ]
    render(<CronTab projectId="p" />)
    expect(screen.getByText('live-job')).toBeInTheDocument()
    expect(screen.getByText('paused-job')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('radio', { name: 'Enabled' }))
    expect(screen.getByText('live-job')).toBeInTheDocument()
    expect(screen.queryByText('paused-job')).toBeNull()

    await userEvent.click(screen.getByRole('radio', { name: 'Disabled' }))
    expect(screen.queryByText('live-job')).toBeNull()
    expect(screen.getByText('paused-job')).toBeInTheDocument()
  })

  it('shows a Load more button when more jobs are available than the page size', async () => {
    cronMock.jobs = Array.from({ length: 25 }, (_, i) =>
      makeJob({ id: `j${i}`, name: `job-${i}` }),
    )
    render(<CronTab projectId="p" />)
    expect(screen.getByRole('button', { name: /load more/i })).toBeInTheDocument()
    expect(screen.getByText('job-19')).toBeInTheDocument()
    expect(screen.queryByText('job-20')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /load more/i }))
    expect(screen.getByText('job-20')).toBeInTheDocument()
    expect(screen.getByText('job-24')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull()
  })

  it('renders the runs list inside the detail pane when a job is selected', () => {
    const job = makeJob({ id: 'sel', name: 'selected-job' })
    cronMock.jobs = [job]
    cronMock.selectedJob = job
    cronMock.runs = [
      {
        id: 'r1',
        cron_job_id: 'sel',
        triggered_at: '2026-05-01T12:00:00Z',
        started_at: null,
        completed_at: null,
        status: 'success',
        output: null,
        error: null,
        agent_run_id: null,
        pipeline_execution_id: null,
        created_at: '2026-05-01T12:00:00Z',
      },
    ]
    render(<CronTab projectId="p" />)
    expect(screen.getByText(/success/i)).toBeInTheDocument()
  })
})
