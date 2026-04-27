import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TokenEfficiencyCard } from '../TokenEfficiencyCard'

const mocks = vi.hoisted(() => ({
  useTokenTimeSeries: vi.fn(),
  useSavings: vi.fn(),
  useUsage: vi.fn(),
  useModelBreakdown: vi.fn(),
}))

vi.mock('../../../hooks/useTokenTimeSeries', () => ({
  useTokenTimeSeries: mocks.useTokenTimeSeries,
}))

vi.mock('../../../hooks/useSavings', () => ({
  useSavings: mocks.useSavings,
}))

vi.mock('../../../hooks/useUsage', () => ({
  useUsage: mocks.useUsage,
}))

vi.mock('../../../hooks/useModelBreakdown', () => ({
  useModelBreakdown: mocks.useModelBreakdown,
}))

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  AreaChart: ({ children }: { children: ReactNode }) => (
    <div data-testid="area-chart">{children}</div>
  ),
  Area: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Legend: () => null,
  Tooltip: ({
    formatter,
  }: {
    formatter: (
      value: number,
      name: string,
      item: { payload: { tokens_spent: number; tokens_saved: number } },
    ) => [string, string]
  }) => {
    const [savedValue, savedLabel] = formatter(0, 'tokens_saved', {
      payload: { tokens_spent: 1200, tokens_saved: 800 },
    })
    return (
      <div data-testid="tooltip-saved">
        {savedLabel}: {savedValue}
      </div>
    )
  },
}))

function primeHooks() {
  mocks.useTokenTimeSeries.mockReturnValue({
    data: {
      hours: 6,
      granularity: '30m',
      buckets: [
        {
          timestamp: '2026-04-08T12:00:00Z',
          tokens_spent: 1200,
          tokens_saved: 800,
        },
      ],
    },
    isLoading: false,
    error: null,
  })
  mocks.useSavings.mockReturnValue({
    data: { total_tokens_saved: 800, categories: {} },
  })
  mocks.useUsage.mockReturnValue({
    data: {
      totals: {
        input_tokens: 700,
        output_tokens: 500,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        session_count: 1,
      },
      by_source: {},
      by_model: {},
    },
  })
  mocks.useModelBreakdown.mockReturnValue({ data: [] })
}

describe('TokenEfficiencyCard phase 2 red coverage', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('formats the Saved tooltip from the bucket tokens_saved value', () => {
    primeHooks()

    render(<TokenEfficiencyCard hours={6} />)

    expect(screen.getByTestId('tooltip-saved')).toHaveTextContent('Saved: 800')
  })

  it.each([
    [6, '30m'],
    [168, '1h'],
    [169, '1d'],
  ] as const)('derives %s hour granularity as %s without rendering a toggle', (hours, granularity) => {
    primeHooks()

    render(<TokenEfficiencyCard hours={hours} />)

    expect(mocks.useTokenTimeSeries).toHaveBeenCalledWith(hours, undefined, granularity)
    expect(screen.queryByLabelText('Token chart granularity')).not.toBeInTheDocument()
  })
})
