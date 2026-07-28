import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { ApprovalOption } from '../../../types/chat'
import { PlanApprovalActions } from '../PlanApprovalActions'

// The uniform accept set: Approve (YOLO) dominant primary, Approve (Act) tinted.
const YOLO_ACT_OPTIONS: ApprovalOption[] = [
  { id: 'approve_yolo', label: 'Approve (YOLO)', decision: 'approve', emphasis: 'primary' },
  { id: 'approve_act', label: 'Approve (Act)', decision: 'approve', emphasis: 'accent' },
]

describe('PlanApprovalActions', () => {
  it('renders a fallback approve + reject when no options are supplied', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} testIdPrefix="x" />,
    )
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.getByTestId('x-reject')).toBeInTheDocument()
  })

  it('falls back to a single Approve when options is empty', () => {
    render(
      <PlanApprovalActions onApprove={vi.fn()} onRequestChanges={vi.fn()} options={[]} testIdPrefix="x" />,
    )
    expect(screen.getByTestId('x-approve')).toBeInTheDocument()
    expect(screen.queryByTestId('x-option-approve_yolo')).not.toBeInTheDocument()
  })

  it('renders one button per accept option (YOLO / Act) plus Reject', () => {
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    // The generic single-Approve is replaced by per-option buttons.
    expect(screen.queryByTestId('x-approve')).not.toBeInTheDocument()
    expect(screen.getByTestId('x-option-approve_yolo')).toBeInTheDocument()
    expect(screen.getByTestId('x-option-approve_act')).toBeInTheDocument()
    // Reject stays available alongside the option set.
    expect(screen.getByTestId('x-reject')).toBeInTheDocument()
  })

  it('maps emphasis to button hierarchy: YOLO solid primary, Act tinted accent', () => {
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    // primary = solid accent slab; accent = tinted accent (token-driven classes).
    expect(screen.getByTestId('x-option-approve_yolo').className.split(/\s+/)).toContain('bg-accent')
    expect(screen.getByTestId('x-option-approve_act').className.split(/\s+/)).toContain('bg-accent-tint')
  })

  it('holds the single-accent contract: exactly one solid primary, however many approve options (#15680)', () => {
    // .impeccable single-accent: only the emphasis:'primary' option may render
    // the solid accent CTA. Every other approve option is a tinted secondary,
    // never a peer primary — even if a CLI ever surfaces three+ accept variants.
    // Guards against the original defect (every ExitPlanMode option rendered
    // primary -> three accent buttons in a row for Droid).
    const MULTI: ApprovalOption[] = [
      { id: 'approve_yolo', label: 'Approve (YOLO)', decision: 'approve', emphasis: 'primary' },
      { id: 'approve_act', label: 'Approve (Act)', decision: 'approve', emphasis: 'accent' },
      { id: 'approve_edits', label: 'Approve (auto-edits)', decision: 'approve', emphasis: 'accent' },
    ]
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={MULTI}
        testIdPrefix="x"
      />,
    )
    // The solid-accent slab (`bg-accent`) is unique to the `primary` variant;
    // the tinted `accent` variant uses `bg-accent-tint` (exact-token match so
    // the tinted class can never count as a solid primary).
    const solidPrimaries = MULTI.map((o) =>
      screen.getByTestId(`x-option-${o.id}`),
    ).filter((el) => el.className.split(/\s+/).includes('bg-accent'))
    expect(solidPrimaries).toHaveLength(1)
    expect(solidPrimaries[0]).toBe(screen.getByTestId('x-option-approve_yolo'))
    // Remaining approve options are tinted, not peer primaries.
    expect(screen.getByTestId('x-option-approve_act').className.split(/\s+/)).toContain('bg-accent-tint')
    expect(screen.getByTestId('x-option-approve_edits').className.split(/\s+/)).toContain('bg-accent-tint')
  })

  it('selecting an option calls onApprove with that option', () => {
    const onApprove = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={onApprove}
        onRequestChanges={vi.fn()}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-option-approve_yolo'))
    expect(onApprove).toHaveBeenCalledWith(YOLO_ACT_OPTIONS[0])
  })

  it('reject opens the comment box and submits with the entered comment', () => {
    const onRequestChanges = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={onRequestChanges}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-reject'))
    fireEvent.change(screen.getByTestId('x-feedback'), { target: { value: 'tweak step 2' } })
    fireEvent.click(screen.getByTestId('x-send'))
    expect(onRequestChanges).toHaveBeenCalledWith('tweak step 2')
  })

  it('does not submit reject while Enter is composing text', () => {
    const onRequestChanges = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={onRequestChanges}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-reject'))
    const feedback = screen.getByTestId('x-feedback')
    fireEvent.change(feedback, { target: { value: 'IME input' } })

    const enter = new KeyboardEvent('keydown', {
      key: 'Enter',
      bubbles: true,
      cancelable: true,
    })
    Object.defineProperty(enter, 'isComposing', { value: true })
    fireEvent(feedback, enter)

    expect(onRequestChanges).not.toHaveBeenCalled()
    expect(screen.getByTestId('x-feedback')).toBeInTheDocument()
  })

  it('reject comment is optional: an empty submit still rejects', () => {
    const onRequestChanges = vi.fn()
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={onRequestChanges}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    fireEvent.click(screen.getByTestId('x-reject'))
    // No comment typed — the submit button is enabled and rejects with "".
    const send = screen.getByTestId('x-send')
    expect(send).not.toBeDisabled()
    fireEvent.click(send)
    expect(onRequestChanges).toHaveBeenCalledWith('')
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

  it('stacked layout makes the options 2-up and Reject full-width with touch targets', () => {
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={YOLO_ACT_OPTIONS}
        layout="stacked"
        testIdPrefix="x"
      />,
    )
    const yolo = screen.getByTestId('x-option-approve_yolo')
    const reject = screen.getByTestId('x-reject')
    // Approve options fill their grid column with a 44px touch target.
    expect(yolo.className).toContain('w-full')
    expect(yolo.className).toContain('pointer-coarse:min-h-11')
    // The two approve options share a 2-up grid row.
    expect(yolo.parentElement?.className).toContain('grid-cols-2')
    // Reject drops to its own full-width row below the grid.
    expect(reject.className).toContain('w-full')
    expect(reject.className).toContain('pointer-coarse:min-h-11')
    expect(reject.parentElement?.className).not.toContain('grid-cols-2')
  })

  it('inline layout (default) keeps one wrapping row and no full-width buttons', () => {
    render(
      <PlanApprovalActions
        onApprove={vi.fn()}
        onRequestChanges={vi.fn()}
        options={YOLO_ACT_OPTIONS}
        testIdPrefix="x"
      />,
    )
    const yolo = screen.getByTestId('x-option-approve_yolo')
    expect(yolo.className).not.toContain('w-full')
    expect(yolo.parentElement?.className).toContain('flex-wrap')
    // Reject shares the same wrapping row as the options.
    expect(screen.getByTestId('x-reject').parentElement?.className).toContain('flex-wrap')
  })
})
