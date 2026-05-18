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
    expect(screen.getByLabelText('Status: Ready')).toBeTruthy()
  })

  it('renders with title', () => {
    render(<StatusDot status="needs_review" />)
    expect(screen.getByTitle('Needs Review')).toBeTruthy()
  })

  it('uses the canonical task-state kind without a fallback branch', () => {
    render(<StatusDot task={{ state: { is_closed: true } }} />)
    expect(screen.getByLabelText('Status: Closed')).toHaveAttribute('data-kind', 'disabled')
  })
})

describe('StatusDot — honest task-state glyphs (#14769 / D2)', () => {
  // status string -> [expected data-glyph, expected data-kind].
  // data-kind is the OKLCH lightness band; it must NOT be remapped, so
  // in_progress stays 'warning' (L≈78) and blocked stays 'error' (L≈65)
  // even though their shapes are now honest.
  const cases: Array<[string, string, string]> = [
    ['open', 'circle', 'info'],
    ['in_progress', 'activity', 'warning'],
    ['needs_review', 'eye', 'info'],
    ['blocked', 'lock', 'error'],
    ['review_approved', 'check', 'success'],
    ['closed', 'dash', 'disabled'],
  ]

  it('maps every state to a distinct, non-alarm shape with its lightness band intact', () => {
    const seenGlyphs = new Set<string>()
    for (const [status, glyph, kind] of cases) {
      const { container, unmount } = render(<StatusDot status={status} />)
      const span = container.querySelector(
        'span.activity-row-status-dot',
      )
      expect(span).not.toBeNull()
      if (!span) throw new Error('Status dot span not found')
      const svg = span.querySelector('svg')
      expect(svg?.getAttribute('data-glyph')).toBe(glyph)
      expect(span.getAttribute('data-kind')).toBe(kind)
      seenGlyphs.add(glyph)
      unmount()
    }
    expect(seenGlyphs.size).toBe(cases.length)
  })

  it('never paints in_progress as a caution triangle or blocked as a failure X', () => {
    const { container: working } = render(<StatusDot status="in_progress" />)
    const workingSvg = working.querySelector('svg')
    expect(workingSvg?.getAttribute('data-glyph')).toBe('activity')
    expect(workingSvg?.querySelector('rect')).toBeNull()

    const { container: blocked } = render(<StatusDot status="blocked" />)
    const blockedSvg = blocked.querySelector('svg')
    expect(blockedSvg?.getAttribute('data-glyph')).toBe('lock')
    // The lock has a rect body and no circle; the old error X was a circle
    // with two crossing strokes.
    expect(blockedSvg?.querySelector('rect')).not.toBeNull()
    expect(blockedSvg?.querySelector('circle')).toBeNull()
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
