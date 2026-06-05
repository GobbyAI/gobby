import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ApprovalOption } from '../../../types/chat'
import { PlanApprovalActions } from '../PlanApprovalActions'

const CLAUDE_OPTIONS: ApprovalOption[] = [
  { id: 'approve_manual', label: 'Approve, manually approve edits', decision: 'approve' },
  { id: 'approve_bypass', label: 'Approve, bypass permissions', decision: 'approve' },
  { id: 'ultraplan', label: 'Refine with Ultraplan', decision: 'keep_planning' },
]

describe('PlanApprovalActions', () => {
  it('renders approve / request-changes for every CLI', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} testIdPrefix="x" />,
    )
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.getByTestId('x-request-changes')).toBeInTheDocument()
  })

  it('falls back to a single Approve when no options are supplied', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} options={[]} testIdPrefix="x" />,
    )
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.queryByTestId('x-option-approve_manual')).not.toBeInTheDocument()
  })

  it("renders one button per CLI option (Claude's ExitPlanMode choices)", () => {
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={CLAUDE_OPTIONS}
        testIdPrefix="x"
      />,
    )
    // The generic single-Approve is replaced by per-option buttons.
    expect(screen.queryByTestId('x-approve')).not.toBeInTheDocument()
    expect(screen.getByTestId('x-option-approve_manual')).toBeInTheDocument()
    expect(screen.getByTestId('x-option-approve_bypass')).toBeInTheDocument()
    expect(screen.getByTestId('x-option-ultraplan')).toBeInTheDocument()
    // Request Changes stays available alongside the option set.
    expect(screen.getByTestId('x-request-changes')).toBeInTheDocument()
  })

  it('selecting an option calls onApprove with that option', () => {
    const onApprove = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={onApprove}
        onRequestChanges={vi.fn()}
        options={CLAUDE_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-option-approve_bypass'))
    expect(onApprove).toHaveBeenCalledWith(CLAUDE_OPTIONS[1])
  })

  it('a keep_planning option is rendered and selectable', () => {
    const onApprove = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={onApprove}
        onRequestChanges={vi.fn()}
        options={CLAUDE_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-option-ultraplan'))
    expect(onApprove).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ultraplan', decision: 'keep_planning' }),
    )
  })

  it('request-changes path is unchanged with options present', () => {
    const onRequestChanges = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={onRequestChanges}
        options={CLAUDE_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-request-changes'))
    fireEvent.change(screen.getByTestId('x-feedback'), { target: { value: 'tweak step 2' } })
    fireEvent.click(screen.getByTestId('x-send'))
    expect(onRequestChanges).toHaveBeenCalledWith('tweak step 2')
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
