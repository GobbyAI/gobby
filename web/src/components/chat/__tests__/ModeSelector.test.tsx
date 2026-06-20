import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ModeSelector } from '../ModeSelector'

describe('ModeSelector', () => {
  it('disables the Plan radio while disabled (e.g. a plan is pending approval)', () => {
    render(<ModeSelector mode="plan" onModeChange={vi.fn()} disabled />)
    expect(screen.getByRole('radio', { name: 'Plan' })).toBeDisabled()
  })

  it('keeps the Plan radio enabled when not disabled', () => {
    render(<ModeSelector mode="plan" onModeChange={vi.fn()} />)
    expect(screen.getByRole('radio', { name: 'Plan' })).not.toBeDisabled()
  })

  it('opts the compact chat toolbar selector out of coarse pointer touch targets', () => {
    render(<ModeSelector mode="plan" onModeChange={vi.fn()} />)

    expect(screen.getByRole('radiogroup', { name: 'Chat mode' })).not.toHaveClass(
      'pointer-coarse:min-h-11',
    )
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio).not.toHaveClass('pointer-coarse:min-h-11')
      expect(radio).not.toHaveClass('pointer-coarse:min-w-11')
    }
  })
})
