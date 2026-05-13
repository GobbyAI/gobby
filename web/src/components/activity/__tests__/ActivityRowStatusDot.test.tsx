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
  'disabled',
]

const TOKENS: Record<StatusKind, string> = {
  success: 'var(--color-success-foreground)',
  info: 'var(--color-info)',
  warning: 'var(--color-warning-foreground)',
  error: 'var(--color-error)',
  disabled: 'var(--text-muted)',
}

// One distinct SVG primitive per kind. The contract here is that recolouring
// the dot to gray must still leave the indicator legible — so we assert each
// kind paints a different geometric shape, not just a different fill.
const PRIMARY_TAG: Record<StatusKind, string> = {
  success: 'circle',
  info: 'polygon',
  warning: 'polygon',
  error: 'path',
  disabled: 'rect',
}

function renderDot(kind: StatusKind, extra: { pulse?: boolean; label?: string; title?: string } = {}) {
  const { container } = render(<ActivityRowStatusDot kind={kind} {...extra} />)
  const svg = container.querySelector('svg.activity-row-status-dot')
  if (!svg) throw new Error(`No status dot rendered for kind=${kind}`)
  return svg
}

describe('ActivityRowStatusDot — deutan-safe state rendering (#14586)', () => {
  it('renders an SVG with the data-kind attribute for every kind', () => {
    for (const kind of KINDS) {
      const svg = renderDot(kind)
      expect(svg.getAttribute('data-kind')).toBe(kind)
    }
  })

  it('paints a distinct geometric primitive for each kind', () => {
    // Five kinds, five distinct first-child tag names. Shared shapes between
    // kinds would mean the dot is hue-only — the regression we are guarding.
    const primaryTags = new Set<string>()
    for (const kind of KINDS) {
      const svg = renderDot(kind)
      // Skip <title> if present, take the first geometric child.
      const child = Array.from(svg.children).find(
        (el) => el.tagName.toLowerCase() !== 'title',
      )
      if (!child) throw new Error(`No glyph rendered for kind=${kind}`)
      const tag = child.tagName.toLowerCase()
      expect(tag).toBe(PRIMARY_TAG[kind])
      primaryTags.add(`${kind}:${tag}:${child.outerHTML}`)
    }
    expect(primaryTags.size).toBe(KINDS.length)
  })

  it('binds each kind to a distinct OKLCH lightness token', () => {
    const seenTokens = new Set<string>()
    for (const kind of KINDS) {
      const svg = renderDot(kind)
      const color = (svg as SVGElement).style.color
      expect(color).toBeTruthy()
      // jsdom preserves the var() expression verbatim on inline style.color.
      expect(color).toContain(TOKENS[kind].slice(4, -1)) // token name between `var(` and `)`
      seenTokens.add(color)
    }
    expect(seenTokens.size).toBe(KINDS.length)
  })

  it('exposes the label as an accessible name with role=img', () => {
    const svg = renderDot('success', { label: 'OK' })
    expect(svg.getAttribute('aria-label')).toBe('OK')
    expect(svg.getAttribute('role')).toBe('img')
  })

  it('hides the dot from assistive tech when no label is provided', () => {
    const svg = renderDot('disabled')
    expect(svg.getAttribute('aria-hidden')).toBe('true')
    expect(svg.getAttribute('role')).toBeNull()
  })

  it('applies the pulse class when pulse=true', () => {
    const svg = renderDot('info', { pulse: true, label: 'Running' })
    expect(svg.getAttribute('class')).toContain('activity-row-status-dot--pulse')
  })

  it('emits a <title> element when title prop is set', () => {
    const svg = renderDot('warning', { title: 'Timeout', label: 'Timeout' })
    const title = svg.querySelector('title')
    expect(title?.textContent).toBe('Timeout')
  })
})
