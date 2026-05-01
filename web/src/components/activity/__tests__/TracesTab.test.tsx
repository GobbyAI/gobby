import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach } from 'vitest'

import { TracesTab } from '../TracesTab'
import type { TraceRecord, SpanRecord } from '../../../hooks/useTraces'

const tracesMock = vi.hoisted(() => ({
  traces: [] as TraceRecord[],
  isLoading: false,
  filters: {},
  setFilters: vi.fn(),
  fetchTraces: vi.fn(),
  selectedTraceId: null as string | null,
  setSelectedTraceId: vi.fn(),
}))

const detailMock = vi.hoisted(() => ({
  spans: [] as SpanRecord[],
  isLoading: false,
  fetchDetail: vi.fn(),
}))

vi.mock('../../../hooks/useTraces', () => ({
  useTraces: () => tracesMock,
  useTraceDetail: () => detailMock,
}))

vi.mock('../../chat/artifacts/ResizeHandle', () => ({
  ResizeHandle: () => null,
}))

function makeTrace(overrides: Partial<TraceRecord> = {}): TraceRecord {
  return {
    id: 'r-1',
    project_id: 'p',
    trace_id: 'trace-1',
    root_span_name: 'GET /api',
    status: 'OK',
    start_time_ns: 0,
    end_time_ns: 0,
    duration_ms: 12.34,
    timestamp: '2026-05-01T12:00:00Z',
    ...overrides,
  }
}

function makeSpan(overrides: Partial<SpanRecord> = {}): SpanRecord {
  return {
    id: 's-1',
    trace_id: 'trace-1',
    span_id: 'span-1',
    parent_id: null,
    name: 'span-name',
    kind: 'internal',
    status: 'OK',
    start_time_ns: 0,
    end_time_ns: 1_000_000,
    attributes_json: null,
    events_json: null,
    ...overrides,
  }
}

beforeEach(() => {
  tracesMock.traces = []
  tracesMock.isLoading = false
  tracesMock.selectedTraceId = null
  tracesMock.setSelectedTraceId = vi.fn()
  detailMock.spans = []
  detailMock.isLoading = false
})

describe('TracesTab', () => {
  it('renders an empty state when no traces are loaded', () => {
    render(<TracesTab projectId="p" />)
    expect(screen.getByText(/no traces/i)).toBeInTheDocument()
  })

  it('sorts traces newest-first and calls setSelectedTraceId on click', async () => {
    tracesMock.traces = [
      makeTrace({ trace_id: 't-old', root_span_name: 'old-span', timestamp: '2026-04-01T00:00:00Z' }),
      makeTrace({ trace_id: 't-new', root_span_name: 'new-span', timestamp: '2026-05-01T00:00:00Z' }),
    ]
    render(<TracesTab projectId="p" />)
    const buttons = screen.getAllByTestId('trace-row-button')
    expect(buttons[0]).toHaveTextContent('new-span')

    await userEvent.click(buttons[1])
    expect(tracesMock.setSelectedTraceId).toHaveBeenCalledWith('t-old')
  })

  it('shows a Load more button when more traces are available than the page size', async () => {
    tracesMock.traces = Array.from({ length: 25 }, (_, i) =>
      makeTrace({
        trace_id: `t-${i}`,
        root_span_name: `span-${i}`,
        timestamp: new Date(2026, 0, 25 - i).toISOString(),
      }),
    )
    render(<TracesTab projectId="p" />)
    expect(screen.getByRole('button', { name: /load more/i })).toBeInTheDocument()
    expect(screen.getByText('span-0')).toBeInTheDocument()
    expect(screen.queryByText('span-20')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: /load more/i }))
    expect(screen.getByText('span-20')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /load more/i })).toBeNull()
  })

  it('renders the spans list inside the detail pane when a trace is selected', () => {
    const trace = makeTrace({ trace_id: 'sel', root_span_name: 'selected-trace' })
    tracesMock.traces = [trace]
    tracesMock.selectedTraceId = 'sel'
    detailMock.spans = [makeSpan({ id: 's-a', name: 'inner-span' })]
    render(<TracesTab projectId="p" />)
    expect(screen.getByText('inner-span')).toBeInTheDocument()
  })
})
