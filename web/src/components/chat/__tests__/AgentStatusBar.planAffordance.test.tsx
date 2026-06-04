import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AgentStatusBar } from '../AgentStatusBar'

describe('AgentStatusBar pending-plan affordance', () => {
  it('shows the pending strip inside the existing bar (not a second bar) when pending', () => {
    render(
      <AgentStatusBar
        interactionMode="none"
        planPendingApproval
        onApprovePlan={vi.fn()}
        onRequestPlanChanges={vi.fn()}
        onViewPlan={vi.fn()}
      />,
    )

    const bar = screen.getByTestId('agent-status-bar')
    const strip = screen.getByTestId('plan-pending-strip')
    expect(strip).toBeInTheDocument()
    // The affordance is a pending state of the existing strip, not a second
    // docked bar.
    expect(bar.contains(strip)).toBe(true)
    expect(screen.getAllByTestId('agent-status-bar')).toHaveLength(1)

    expect(screen.getByTestId('plan-strip-approve')).toBeInTheDocument()
    expect(screen.getByTestId('plan-strip-request-changes')).toBeInTheDocument()
    expect(screen.getByTestId('plan-strip-view')).toBeInTheDocument()
  })

  it('does not show the strip when no plan is pending', () => {
    render(
      <AgentStatusBar
        interactionMode="none"
        onApprovePlan={vi.fn()}
        onRequestPlanChanges={vi.fn()}
      />,
    )
    expect(screen.queryByTestId('plan-pending-strip')).not.toBeInTheDocument()
  })
})
