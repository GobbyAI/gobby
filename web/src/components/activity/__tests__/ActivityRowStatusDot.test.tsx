import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import {
  ActivityRowStatusDot,
  type StatusKind,
} from '../ActivityRowStatusDot'

const KINDS: StatusKind[] = [
  'success',
  'info',
  'warning',
  'error',
  'paused',
  'stopped',
  'disabled',
]

const TOKENS: Record<StatusKind, string> = {
  success: 'var(--color-success-foreground)',
  info: 'var(--color-info)',
  warning: 'var(--color-warning-foreground)',
  error: 'var(--color-error)',
  paused: 'var(--text-secondary)',
  stopped: 'var(--color-inactive)',
  disabled: 'var(--text-muted)',
}

// Each kind must paint a different local glyph. The class name carries the
// icon identity, which is what we assert on — recolouring to gray cannot
// collapse two kinds onto the same shape.
const ICON_CLASS: Record<StatusKind, string> = {
  success: 'activity-row-status-dot__glyph--success',
  info: 'activity-row-status-dot__glyph--info',
  warning: 'activity-row-status-dot__glyph--warning',
  error: 'activity-row-status-dot__glyph--error',
  paused: 'activity-row-status-dot__glyph--paused',
  stopped: 'activity-row-status-dot__glyph--stopped',
  disabled: 'activity-row-status-dot__glyph--disabled',
}

function renderDot(
  kind: StatusKind,
  extra: { pulse?: boolean; label?: string; title?: string } = {},
) {
  const { container } = render(<ActivityRowStatusDot kind={kind} {...extra} />)
  const span = container.querySelector('span.activity-row-status-dot')
  if (!span) throw new Error(`No status dot rendered for kind=${kind}`)
  return span as HTMLSpanElement
}

describe('ActivityRowStatusDot — deutan-safe state rendering (#14586)', () => {
  it('renders a span with the data-kind attribute for every kind', () => {
    for (const kind of KINDS) {
      const span = renderDot(kind)
      expect(span.getAttribute('data-kind')).toBe(kind)
    }
  })

  it('paints a distinct local glyph for each kind', () => {
    const seenClasses = new Set<string>()
    for (const kind of KINDS) {
      const span = renderDot(kind)
      const svg = span.querySelector('svg')
      if (!svg) throw new Error(`No icon rendered for kind=${kind}`)
      const cls = svg.getAttribute('class') ?? ''
      expect(cls).toContain('activity-row-status-dot__glyph')
      expect(cls).toContain(ICON_CLASS[kind])
      seenClasses.add(ICON_CLASS[kind])
    }
    // Each kind gets a distinct local glyph; uniqueness is the guard
    // against any future regression to a hue-only indicator.
    expect(seenClasses.size).toBe(KINDS.length)
  })

  it('binds each kind to a distinct OKLCH lightness token', () => {
    const seenTokens = new Set<string>()
    for (const kind of KINDS) {
      const span = renderDot(kind)
      const color = span.style.color
      expect(color).toBeTruthy()
      expect(color).toContain(TOKENS[kind].slice(4, -1)) // strip leading `var(` and trailing `)`
      seenTokens.add(color)
    }
    expect(seenTokens.size).toBe(KINDS.length)
  })

  it('exposes the label as an accessible name with role=img', () => {
    const span = renderDot('success', { label: 'OK' })
    expect(span.getAttribute('aria-label')).toBe('OK')
    expect(span.getAttribute('role')).toBe('img')
    expect(span.getAttribute('aria-hidden')).toBe('false')
  })

  it('hides the dot from assistive tech when no label is provided', () => {
    const span = renderDot('disabled')
    expect(span.getAttribute('aria-hidden')).toBe('true')
    expect(span.getAttribute('role')).toBeNull()
  })

  it('applies the pulse class when pulse=true', () => {
    const span = renderDot('info', { pulse: true, label: 'Running' })
    expect(span.getAttribute('class')).toContain(
      'activity-row-status-dot--pulse',
    )
  })

  it('echoes the title prop on the wrapper for tooltip surfaces', () => {
    const span = renderDot('warning', { title: 'Timeout', label: 'Timeout' })
    expect(span.getAttribute('title')).toBe('Timeout')
  })
})

// ----------------------------------------------------------------------
// Grayscale-filter snapshot guard. The principle: rendering each kind
// inside a `filter: grayscale(1)` wrapper produces structurally distinct
// markup — different local glyph classes, different inline color tokens —
// even when the hue channel is collapsed by the CSS filter.
// ----------------------------------------------------------------------
describe('ActivityRowStatusDot — grayscale(1) structural snapshot', () => {
  function grayscaleDot(kind: StatusKind): SVGElement {
    const { container } = render(
      <div style={{ filter: 'grayscale(1)' }}>
        <ActivityRowStatusDot kind={kind} label={kind} />
      </div>,
    )
    const svg = container.querySelector('svg')
    if (!svg) throw new Error(`No icon rendered for kind=${kind}`)
    return svg
  }

  it('produces distinct, stable structures for all states', () => {
    const seenClasses = new Set<string>()
    for (const kind of KINDS) {
      const svg = grayscaleDot(kind)
      const cls = svg.getAttribute('class') ?? ''
      expect(cls).toContain(ICON_CLASS[kind])
      expect(svg.childElementCount).toBeGreaterThan(0)
      seenClasses.add(cls)
    }

    expect(seenClasses.size).toBe(KINDS.length)
  })
})
