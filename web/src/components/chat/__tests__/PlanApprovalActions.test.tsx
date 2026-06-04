import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PlanApprovalActions } from '../PlanApprovalActions'
import { PlanCapabilityProvider } from '../PlanCapabilityContext'

describe('PlanApprovalActions manual-switch note (1e)', () => {
  it('shows the manual-switch note when the CLI cannot auto-switch', () => {
    render(
      <PlanCapabilityProvider manualSwitchRequired>
        <PlanApprovalActions
          onApprove={vi.fn()}
          onRequestChanges={vi.fn()}
          testIdPrefix="x"
        />
      </PlanCapabilityProvider>,
    )
    expect(screen.getByTestId('plan-manual-switch-note')).toBeInTheDocument()
    // Approve / request-changes still show for every CLI.
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.getByTestId('x-request-changes')).toBeInTheDocument()
  })

  it('hides the note for native CLIs that auto-switch (default)', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} testIdPrefix="x" />,
    )
    expect(screen.queryByTestId('plan-manual-switch-note')).not.toBeInTheDocument()
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
  })
})
