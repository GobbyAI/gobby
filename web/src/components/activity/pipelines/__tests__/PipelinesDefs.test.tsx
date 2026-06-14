import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PipelinesTab } from '../../PipelinesTab'
import { createMockFetch, type MockFetchInstance } from '../../../../test/mocks/fetch'

vi.mock('../../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}))

vi.mock('../../../shared/executions/execution-utils', () => ({
  PipelineStatusDot: ({ status }: { status: string }) => <span>{status}</span>,
  StepDisplay: () => null,
}))

vi.mock('../../../shared/executions/executionFormatters', () => ({
  formatDateTime: (value: string) => value,
  formatDuration: () => '1m',
}))

let mockFetch: MockFetchInstance

describe('Pipelines defs segment', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    window.localStorage.removeItem('gobby-pipelines-segment-v1')
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
    mockFetch.mockJsonResponse(/\/api\/workflows\?/, {
      definitions: [
        {
          id: 'wf-1',
          name: 'deploy-prod',
          workflow_type: 'pipeline',
          description: 'Deploy production services with staged approvals.',
          definition_json: JSON.stringify({ name: 'deploy-prod', steps: [] }),
          enabled: true,
          source: 'installed',
          priority: 2,
          version: '1.0',
          tags: ['release'],
        },
      ],
    })
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
    mockFetch.restore()
    vi.restoreAllMocks()
    window.localStorage.removeItem('gobby-pipelines-segment-v1')
  })

  it('defaults to Live, switches to Defs, and persists the selected segment', async () => {
    render(<PipelinesTab projectId="project-1" />)

    await waitFor(() => {
      expect(screen.getByText('Nightly sync')).toBeInTheDocument()
    })
    expect(screen.getByRole('radio', { name: 'Live' })).toHaveAttribute('aria-checked', 'true')

    fireEvent.click(screen.getByRole('radio', { name: 'Defs' }))

    await waitFor(() => {
      expect(
        within(screen.getByRole('list', { name: 'Pipeline definitions' })).getByText('deploy-prod'),
      ).toBeInTheDocument()
    })
    // Single-line rows surface the definition name + chips; the description now
    // lives in the detail pane, not inline on the list row.
    expect(
      within(screen.getByRole('list', { name: 'Pipeline definitions' })).queryByText(
        'Deploy production services with staged approvals.',
      ),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Defs' })).toHaveAttribute('aria-checked', 'true')
    expect(window.localStorage.getItem('gobby-pipelines-segment-v1')).toBe('defs')

    const workflowCall = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .find((url) => url.includes('/api/workflows?'))

    expect(workflowCall).toContain('workflow_type=pipeline')
    expect(workflowCall).toContain('include_deleted=true')
    expect(workflowCall).toContain('project_id=project-1')
  })
})
