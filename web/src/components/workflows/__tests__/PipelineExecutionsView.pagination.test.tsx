import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { PipelineExecutionsView } from '../PipelineExecutionsView'

const NOOP = vi.fn().mockResolvedValue(undefined)

const baseProps = {
  executions: [],
  isLoading: false,
  filters: {},
  onFiltersChange: vi.fn(),
  onApprove: NOOP,
  onReject: NOOP,
}

describe('PipelineExecutionsView pagination footer', () => {
  it('omits the footer when pagination props are not provided', () => {
    render(<PipelineExecutionsView {...baseProps} />)
    expect(screen.queryByText(/of /)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()
  })

  it('renders X-Y of N ribbon and disables Prev on the first page', () => {
    const onOffsetChange = vi.fn()
    render(
      <PipelineExecutionsView
        {...baseProps}
        executions={[]}
        total={42}
        limit={10}
        offset={0}
        onOffsetChange={onOffsetChange}
      />,
    )
    expect(screen.getByText(/1.*10.*of.*42/)).toBeInTheDocument()
    const prev = screen.getByRole('button', { name: 'Prev' })
    const next = screen.getByRole('button', { name: 'Next' })
    expect(prev).toBeDisabled()
    expect(next).not.toBeDisabled()
  })

  it('clicking Next advances offset by limit', () => {
    const onOffsetChange = vi.fn()
    render(
      <PipelineExecutionsView
        {...baseProps}
        total={42}
        limit={10}
        offset={0}
        onOffsetChange={onOffsetChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Next' }))
    expect(onOffsetChange).toHaveBeenCalledWith(10)
  })

  it('disables Next on the last page and shows the truncated end value', () => {
    render(
      <PipelineExecutionsView
        {...baseProps}
        total={42}
        limit={10}
        offset={40}
        onOffsetChange={vi.fn()}
      />,
    )
    expect(screen.getByText(/41.*42.*of.*42/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Prev' })).not.toBeDisabled()
  })

  it('clicking Prev decrements offset by limit and clamps to zero', () => {
    const onOffsetChange = vi.fn()
    render(
      <PipelineExecutionsView
        {...baseProps}
        total={42}
        limit={10}
        offset={5}
        onOffsetChange={onOffsetChange}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Prev' }))
    expect(onOffsetChange).toHaveBeenCalledWith(0)
  })

  it('hides the footer when total is zero', () => {
    render(
      <PipelineExecutionsView
        {...baseProps}
        total={0}
        limit={10}
        offset={0}
        onOffsetChange={vi.fn()}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Next' })).not.toBeInTheDocument()
  })
})
