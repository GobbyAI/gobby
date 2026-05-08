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

  function openFilterDropdown(): void {
    fireEvent.click(screen.getByRole('button', { name: 'Filter pipelines' }))
  }

  it('defaults the activity filter to All so transient runs remain visible', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
    })
    openFilterDropdown()
    expect(screen.getByRole('option', { name: 'All' })).toHaveAttribute('aria-selected', 'true')
    await screen.findByTestId('resize-handle')

    const executionCalls = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes('/api/pipelines/executions?'))

    expect(executionCalls.length).toBeGreaterThan(0)
    expect(executionCalls[0]).not.toContain('status=running')
  })

  it('renders dropdown filter options in the expected order', async () => {
    render(<PipelinesTab projectId="proj-1" />)
    await waitFor(() => {
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
    })
    openFilterDropdown()
    const options = screen.getAllByRole('option')
    expect(options.map((option) => option.textContent)).toEqual(['All', 'Completed', 'Failed', 'Running'])
  })

  it('auto-selects the first execution and keeps the detail panel open', async () => {
    render(<PipelinesTab projectId="proj-1" />)

    await waitFor(() => {
      expect(screen.getByTestId('resize-handle')).toBeInTheDocument()
      expect(screen.getByText('No steps available')).toBeInTheDocument()
      expect(screen.queryByText('Close')).toBeNull()
    })
  })

  it('switches filters through the dropdown', async () => {
    render(<PipelinesTab projectId="proj-1" />)
    await waitFor(() => {
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
    })
    openFilterDropdown()

    const failedOption = screen.getByRole('option', { name: 'Failed' })
    fireEvent.click(failedOption)

    openFilterDropdown()
    await waitFor(() => {
      expect(screen.getByRole('option', { name: 'Failed' })).toHaveAttribute('aria-selected', 'true')
      expect(screen.getByRole('option', { name: 'All' })).toHaveAttribute('aria-selected', 'false')
    })
  })
})
