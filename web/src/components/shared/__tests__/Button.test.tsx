import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Button } from '../Button'

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Button', () => {
  it('sets aria-busy only while loading', () => {
    const { rerender } = render(<Button loading>Save</Button>)

    expect(screen.getByRole('button', { name: /save/i })).toHaveAttribute('aria-busy', 'true')

    rerender(<Button>Save</Button>)

    expect(screen.getByRole('button', { name: /save/i })).not.toHaveAttribute('aria-busy')
  })

  it('warns in dev when asChild is combined with loading', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})

    render(
      <Button asChild loading>
        <a href="/tasks">Tasks</a>
      </Button>,
    )

    expect(warn).toHaveBeenCalledWith(
      'Button asChild loading state cannot inject a spinner; render loading UI in the child.',
    )
  })
})
