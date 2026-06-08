import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { type ComponentProps } from 'react'
import { PlanPendingActionStrip } from '../PlanPendingActionStrip'
import { getPlanPendingColors } from '../planPendingSurface'

const planPendingColors = getPlanPendingColors('info')

function renderStrip(overrides: Partial<ComponentProps<typeof PlanPendingActionStrip>> = {}) {
  const props: ComponentProps<typeof PlanPendingActionStrip> = {
    onApprove: vi.fn(),
    onRequestChanges: vi.fn(),
    onView: vi.fn(),
    ...overrides,
  }
  return { ...render(<PlanPendingActionStrip {...props} />), props }
}

describe('PlanPendingActionStrip', () => {
  it('renders the pending state without a side-stripe accent', () => {
    renderStrip()
    const strip = screen.getByTestId('plan-pending-strip')
    expect(screen.getByText('Plan awaiting approval')).toBeInTheDocument()
    // Grayscale-legible: an icon + the shared accent token carry the state,
    // never hue alone.
    expect(strip.querySelector('svg')).toBeTruthy()
    expect(strip.className).toContain(planPendingColors.surfaceBg)
    expect(strip.innerHTML).toContain(planPendingColors.accentText)
    expect(strip.className).not.toContain('border-l')
    expect(strip.className).not.toContain('border-r')
  })

  it('fires onApprove', () => {
    const { props } = renderStrip()
    fireEvent.click(screen.getByTestId('plan-strip-approve'))
    expect(props.onApprove).toHaveBeenCalledTimes(1)
  })

  it('fires onRequestChanges with feedback', () => {
    const { props } = renderStrip()
    fireEvent.click(screen.getByTestId('plan-strip-reject'))
    fireEvent.change(screen.getByTestId('plan-strip-feedback'), {
      target: { value: 'Tweak the rollout step' },
    })
    fireEvent.click(screen.getByTestId('plan-strip-send'))
    expect(props.onRequestChanges).toHaveBeenCalledWith('Tweak the rollout step')
  })

  it('fires onView (focus the Plans panel)', () => {
    const { props } = renderStrip()
    fireEvent.click(screen.getByTestId('plan-strip-view'))
    expect(props.onView).toHaveBeenCalledTimes(1)
  })

  it('uses the runtime amber variant when requested', () => {
    renderStrip({ variant: 'amber' })
    const strip = screen.getByTestId('plan-pending-strip')
    const amber = getPlanPendingColors('amber')
    expect(strip.className).toContain(amber.surfaceBg)
    expect(strip.innerHTML).toContain(amber.accentText)
  })
})
