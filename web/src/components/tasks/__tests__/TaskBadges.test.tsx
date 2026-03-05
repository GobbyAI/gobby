import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusBadge, StatusDot, PriorityBadge, TypeBadge } from '../TaskBadges'

describe('StatusBadge', () => {
  it('renders status text with underscores replaced', () => {
    render(<StatusBadge status="in_progress" />)
    expect(screen.getByText('in progress')).toBeTruthy()
  })

  it('renders known statuses', () => {
    const statuses = ['open', 'in_progress', 'needs_review', 'review_approved', 'closed', 'escalated']
    for (const status of statuses) {
      const { unmount } = render(<StatusBadge status={status} />)
      expect(screen.getByText(status.replace(/_/g, ' '))).toBeTruthy()
      unmount()
    }
  })

  it('handles unknown status gracefully', () => {
    render(<StatusBadge status="unknown_status" />)
    expect(screen.getByText('unknown status')).toBeTruthy()
  })
})

describe('StatusDot', () => {
  it('renders with correct aria-label', () => {
    render(<StatusDot status="open" />)
    expect(screen.getByLabelText('Status: open')).toBeTruthy()
  })

  it('renders with title', () => {
    render(<StatusDot status="needs_review" />)
    expect(screen.getByTitle('needs review')).toBeTruthy()
  })
})

describe('PriorityBadge', () => {
  it('renders priority labels', () => {
    const labels: Record<number, string> = {
      0: 'Critical',
      1: 'High',
      2: 'Medium',
      3: 'Low',
      4: 'Backlog',
    }
    for (const [priority, label] of Object.entries(labels)) {
      const { unmount } = render(<PriorityBadge priority={Number(priority)} />)
      expect(screen.getByText(label)).toBeTruthy()
      unmount()
    }
  })

  it('falls back to Medium for unknown priority', () => {
    render(<PriorityBadge priority={99} />)
    expect(screen.getByText('Medium')).toBeTruthy()
  })
})

describe('TypeBadge', () => {
  it('renders task types', () => {
    const types = ['task', 'bug', 'feature', 'epic', 'chore']
    for (const type of types) {
      const { unmount } = render(<TypeBadge type={type} />)
      expect(screen.getByText(type)).toBeTruthy()
      unmount()
    }
  })

  it('handles unknown type', () => {
    render(<TypeBadge type="custom_type" />)
    expect(screen.getByText('custom_type')).toBeTruthy()
  })
})
