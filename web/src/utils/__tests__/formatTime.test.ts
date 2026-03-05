import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  relativeTime,
  formatRelativeTime,
  formatDuration,
  DURATION_INVALID,
  typeLabel,
  formatTokens,
  formatCost,
} from '../formatTime'

// Helper: produce an ISO string N milliseconds in the past
function ago(ms: number): string {
  return new Date(Date.now() - ms).toISOString()
}

const SECOND = 1_000
const MINUTE = 60 * SECOND
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

afterEach(() => {
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// relativeTime
// ---------------------------------------------------------------------------
describe('relativeTime', () => {
  it('returns "just now" for future dates', () => {
    const future = new Date(Date.now() + 60_000).toISOString()
    expect(relativeTime(future)).toBe('just now')
  })

  it('returns "just now" for <60 seconds ago', () => {
    expect(relativeTime(ago(30 * SECOND))).toBe('just now')
  })

  it('returns minutes ago', () => {
    expect(relativeTime(ago(5 * MINUTE))).toBe('5m ago')
  })

  it('returns hours ago', () => {
    expect(relativeTime(ago(3 * HOUR))).toBe('3h ago')
  })

  it('returns days ago', () => {
    expect(relativeTime(ago(7 * DAY))).toBe('7d ago')
  })

  it('returns months ago for >=30 days', () => {
    expect(relativeTime(ago(60 * DAY))).toBe('2mo ago')
  })
})

// ---------------------------------------------------------------------------
// formatRelativeTime
// ---------------------------------------------------------------------------
describe('formatRelativeTime', () => {
  it('returns "Invalid date" for garbage input', () => {
    expect(formatRelativeTime('nope')).toBe('Invalid date')
  })

  it('returns "now" for <1 minute ago', () => {
    expect(formatRelativeTime(ago(10 * SECOND))).toBe('now')
  })

  it('returns minutes', () => {
    expect(formatRelativeTime(ago(5 * MINUTE))).toBe('5m')
  })

  it('returns hours', () => {
    expect(formatRelativeTime(ago(2 * HOUR))).toBe('2h')
  })

  it('returns days', () => {
    expect(formatRelativeTime(ago(3 * DAY))).toBe('3d')
  })

  it('returns locale date string for >=30 days', () => {
    const result = formatRelativeTime(ago(45 * DAY))
    // Should be a date string, not a relative time
    expect(result).not.toMatch(/^\d+[dhm]$/)
  })
})

// ---------------------------------------------------------------------------
// formatDuration
// ---------------------------------------------------------------------------
describe('formatDuration', () => {
  it('returns DURATION_INVALID for invalid start', () => {
    expect(formatDuration('garbage')).toBe(DURATION_INVALID)
  })

  it('returns DURATION_INVALID for invalid end', () => {
    expect(formatDuration('2024-01-01T00:00:00Z', 'garbage')).toBe(DURATION_INVALID)
  })

  it('returns "<1m" for negative durations', () => {
    expect(formatDuration('2024-01-02T00:00:00Z', '2024-01-01T00:00:00Z')).toBe('<1m')
  })

  it('returns "<1m" for durations under 1 minute', () => {
    expect(formatDuration('2024-01-01T00:00:00Z', '2024-01-01T00:00:30Z')).toBe('<1m')
  })

  it('returns minutes for durations under 1 hour', () => {
    expect(formatDuration('2024-01-01T00:00:00Z', '2024-01-01T00:25:00Z')).toBe('25m')
  })

  it('returns hours and minutes', () => {
    expect(formatDuration('2024-01-01T00:00:00Z', '2024-01-01T02:30:00Z')).toBe('2h 30m')
  })

  it('returns hours only when no remaining minutes', () => {
    expect(formatDuration('2024-01-01T00:00:00Z', '2024-01-01T03:00:00Z')).toBe('3h')
  })

  it('uses Date.now() when endStr is omitted', () => {
    const start = ago(5 * MINUTE)
    const result = formatDuration(start)
    expect(result).toBe('5m')
  })
})

// ---------------------------------------------------------------------------
// typeLabel
// ---------------------------------------------------------------------------
describe('typeLabel', () => {
  it('returns empty string for empty input', () => {
    expect(typeLabel('')).toBe('')
  })

  it('capitalises first letter', () => {
    expect(typeLabel('task')).toBe('Task')
  })

  it('preserves already-capitalised input', () => {
    expect(typeLabel('Bug')).toBe('Bug')
  })

  it('handles single character', () => {
    expect(typeLabel('a')).toBe('A')
  })
})

// ---------------------------------------------------------------------------
// formatTokens
// ---------------------------------------------------------------------------
describe('formatTokens', () => {
  it('returns raw number below 1000', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(999)).toBe('999')
  })

  it('formats thousands with K suffix', () => {
    expect(formatTokens(1_000)).toBe('1.0K')
    expect(formatTokens(1_500)).toBe('1.5K')
    expect(formatTokens(999_999)).toBe('1000.0K')
  })

  it('formats millions with M suffix', () => {
    expect(formatTokens(1_000_000)).toBe('1.0M')
    expect(formatTokens(2_500_000)).toBe('2.5M')
  })
})

// ---------------------------------------------------------------------------
// formatCost
// ---------------------------------------------------------------------------
describe('formatCost', () => {
  it('returns "$0" for zero', () => {
    expect(formatCost(0)).toBe('$0')
  })

  it('returns "<$0.01" for tiny values', () => {
    expect(formatCost(0.001)).toBe('<$0.01')
    expect(formatCost(0.009)).toBe('<$0.01')
  })

  it('formats normal values with 2 decimal places', () => {
    expect(formatCost(0.01)).toBe('$0.01')
    expect(formatCost(1.5)).toBe('$1.50')
    expect(formatCost(99.99)).toBe('$99.99')
  })
})
