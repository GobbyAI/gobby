import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PriorityBadge, StatusBadge, TypeBadge } from '../TaskBadges'

describe('TaskBadges Phase 1 chip contract', () => {
  it('renders status, priority, and type badges through the shared Chip pill', () => {
    render(
      <>
        <StatusBadge status="open" />
        <PriorityBadge priority={2} />
        <TypeBadge type="refactor" />
      </>,
    )

    const open = screen.getByText('open')
    const medium = screen.getByText('Medium')
    const refactor = screen.getByText('refactor')

    for (const chip of [open, medium, refactor]) {
      expect(chip).toHaveClass('rounded-full', 'h-5')
      expect(chip).not.toHaveClass('uppercase')
    }
    expect(open.className).toContain('var(--color-info)')
    expect(medium.className).toContain('var(--color-warning-foreground)')
    expect(refactor.className).toContain('var(--text-muted)')
  })
})
