import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { type ComponentProps } from 'react'
import { PlanPendingApprovalBlock } from '../PlanPendingApprovalBlock'

function renderBlock(overrides: Partial<ComponentProps<typeof PlanPendingApprovalBlock>> = {}) {
  const props: ComponentProps<typeof PlanPendingApprovalBlock> = {
    onApprove: vi.fn(),
    onRequestChanges: vi.fn(),
    onView: vi.fn(),
    ...overrides,
  }
  return { ...render(<PlanPendingApprovalBlock {...props} />), props }
}

describe('PlanPendingApprovalBlock', () => {
  it('renders the warning-state header without a side-stripe accent', () => {
    renderBlock()
    const block = screen.getByTestId('plan-pending-block')
    expect(screen.getByText('Awaiting your approval')).toBeInTheDocument()
    // Grayscale-legible: an icon carries the state, not hue alone.
    expect(block.querySelector('svg')).toBeTruthy()
    expect(block.className).toContain('--color-warning-foreground')
    // BAN 1: no left/right side-stripe accent.
    expect(block.className).not.toContain('border-l')
    expect(block.className).not.toContain('border-r')
  })

  it('is collapsible — toggling hides and shows the actions', () => {
    renderBlock()
    expect(screen.getByTestId('plan-pending-approve')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('plan-pending-toggle'))
    expect(screen.queryByTestId('plan-pending-approve')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('plan-pending-toggle'))
    expect(screen.getByTestId('plan-pending-approve')).toBeInTheDocument()
  })

  it('fires onApprove when approve is clicked', () => {
    const { props } = renderBlock()
    fireEvent.click(screen.getByTestId('plan-pending-approve'))
    expect(props.onApprove).toHaveBeenCalledTimes(1)
  })

  it('fires onRequestChanges with the entered feedback', () => {
    const { props } = renderBlock()
    fireEvent.click(screen.getByTestId('plan-pending-request-changes'))
    fireEvent.change(screen.getByTestId('plan-pending-feedback'), {
      target: { value: 'Redo step 3' },
    })
    fireEvent.click(screen.getByTestId('plan-pending-send'))
    expect(props.onRequestChanges).toHaveBeenCalledWith('Redo step 3')
  })

  it('fires onView (focus the Plans panel) when View plan is clicked', () => {
    const { props } = renderBlock()
    fireEvent.click(screen.getByTestId('plan-pending-view'))
    expect(props.onView).toHaveBeenCalledTimes(1)
  })

  it('omits the View affordance when onView is not provided', () => {
    renderBlock({ onView: undefined })
    expect(screen.queryByTestId('plan-pending-view')).not.toBeInTheDocument()
  })
})
