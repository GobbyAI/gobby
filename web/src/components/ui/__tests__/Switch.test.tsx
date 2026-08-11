import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { Switch } from '../Switch'

describe('Switch', () => {
  it('uses a coarse-pointer 44px hit area without resizing the visual track', () => {
    render(
      <Switch
        checked={false}
        onChange={vi.fn()}
        aria-label="Enable feature"
      />,
    )

    const control = screen.getByRole('switch', { name: 'Enable feature' })
    expect(control).toHaveClass('h-6', 'w-11', 'pointer-coarse:h-11')
    expect(control.firstElementChild).toHaveClass('h-6', 'w-11')
  })

  it('keeps the off state visible: mid-tone knob on a tertiary track (#20047)', () => {
    render(
      <Switch checked={false} onChange={vi.fn()} aria-label="Requires human" />,
    )

    const track = screen.getByRole('switch', { name: 'Requires human' })
      .firstElementChild
    expect(track).toHaveClass('bg-[var(--bg-tertiary)]')
    expect(track?.firstElementChild).toHaveClass('bg-[var(--text-muted)]')
    expect(track?.firstElementChild).not.toHaveClass('bg-background')
  })
})
