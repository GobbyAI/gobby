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
})
