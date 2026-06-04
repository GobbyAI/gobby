import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PlanApprovalActions } from '../PlanApprovalActions'

describe('PlanApprovalActions', () => {
  it('renders approve / request-changes for every CLI', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} testIdPrefix="x" />,
    )
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.getByTestId('x-request-changes')).toBeInTheDocument()
  })

  it('never shows a manual-switch note (approval now executes on every CLI)', () => {
    // #15633 removed the "stays in plan mode after approval" degradation:
    // managed CLIs auto-continue execution on approve, so no continue hint.
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} testIdPrefix="x" />,
    )
    expect(screen.queryByTestId('plan-manual-switch-note')).not.toBeInTheDocument()
    expect(screen.queryByText(/stays in plan mode after approval/i)).not.toBeInTheDocument()
  })
})
