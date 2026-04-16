import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { PipelinesTab } from '../PipelinesTab'
import { createMockFetch, type MockFetchInstance } from '../../../test/mocks/fetch'

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}))

vi.mock('../../workflows/execution-utils', () => ({
  PipelineStatusDot: ({ status }: { status: string }) => <span>{status}</span>,
  StepDisplay: () => null,
  formatDateTime: (value: string) => value,
  formatDuration: () => '1m',
}))

let mockFetch: MockFetchInstance

describe('PipelinesTab', () => {
  beforeEach(() => {
    // Fake timers prevent the component's 3s polling interval from firing
    // unpredictably while the test runs. We never advance time in this test
    // — we just want polls to be deterministic, not to fire spontaneously.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch = createMockFetch()
    mockFetch.mockJsonResponse(/\/api\/pipelines\/executions\?/, {
      executions: [
        {
          id: 'exec-1',
          pipeline_name: 'Nightly sync',
          status: 'running',
          created_at: '2026-04-09T00:00:00Z',
        },
      ],
    })
    mockFetch.mockJsonResponse('/api/pipelines/exec-1', {
      execution: {
        id: 'exec-1',
        pipeline_name: 'Nightly sync',
        status: 'running',
        created_at: '2026-04-09T00:00:00Z',
        steps: [],
      },
    })
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    mockFetch.restore()
    vi.restoreAllMocks()
  })

  it('defaults the activity filter to All so transient runs remain visible', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByRole('radio', { name: 'All' })).toHaveAttribute('aria-checked', 'true')
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
    })

    const executionCalls = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes('/api/pipelines/executions?'))

    expect(executionCalls.length).toBeGreaterThan(0)
    expect(executionCalls[0]).not.toContain('status=running')
  })

  it('renders segmented filter buttons in the expected order', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    const radios = await screen.findAllByRole('radio')
    expect(radios.map((radio) => radio.textContent)).toEqual(['All', 'Completed', 'Failed', 'Running'])
  })

  it('auto-selects the first execution and keeps the detail panel open', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
      expect(screen.queryByText('Close')).toBeNull()
    })
  })

  it('switches filters through the segmented control', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    const failedButton = await screen.findByRole('radio', { name: 'Failed' })
    fireEvent.click(failedButton)

    await waitFor(() => {
      expect(failedButton).toHaveAttribute('aria-checked', 'true')
    })
  })
})
