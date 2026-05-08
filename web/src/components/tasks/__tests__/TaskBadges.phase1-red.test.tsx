import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { PriorityBadge, StatusBadge, TypeBadge } from '../TaskBadges'

describe('TaskBadges Phase 1 chip contract', () => {
  it('renders status, priority, and type badges as shared chips', () => {
    render(
      <>
        <StatusBadge status="open" />
        <PriorityBadge priority={2} />
        <TypeBadge type="refactor" />
      </>,
    )

    expect(screen.getByText('open')).toHaveClass('chip', 'chip--state-open')
    expect(screen.getByText('Medium')).toHaveClass('chip', 'chip--priority-2')
    expect(screen.getByText('refactor')).toHaveClass('chip', 'chip--type-refactor')
  })
})
