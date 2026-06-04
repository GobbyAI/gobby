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
})
