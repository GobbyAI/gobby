import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
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

const EXPECTED_DEFAULT_LIGHTNESS: Array<[string, number]> = [
  ['--color-warning-foreground', 78],
  ['--color-success-foreground', 72],
  ['--color-info', 70],
  ['--text-secondary', 68],
  ['--color-error', 65],
  ['--text-muted', 62],
  ['--color-inactive', 60],
]

function defaultThemeTokens(): string {
  // Intentional dependency: ActivityRowStatusDot binds directly to app-level
  // design tokens, and the app-level defaults live in styles/index.css :root.
  // This test reads that block rather than generating CSS so it catches drift
  // between the component token map and the shipped default theme.
  const testDir = dirname(fileURLToPath(import.meta.url))
  const css = readFileSync(join(testDir, '../../../styles/index.css'), 'utf8')
  const match = css.match(/^:root\s*{([\s\S]*?)^}/m)
  if (!match) throw new Error('Unable to find :root token block')
  return match[1]
}

function tokenLightness(block: string, token: string): number {
  // Expected format is `--token-name: oklch(<lightness>% ...);`.
  // The lightness ladder is the accessibility contract under grayscale
  // rendering; changing token syntax should update this assertion deliberately.
  const escapedToken = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = block.match(new RegExp(`${escapedToken}:\\s*oklch\\((\\d+(?:\\.\\d+)?)%`))
  if (!match) throw new Error(`Unable to find OKLCH token ${token}`)
  return Number(match[1])
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
      expect(color).toBe(TOKENS[kind])
      seenTokens.add(color)
    }
    expect(seenTokens.size).toBe(KINDS.length)
  })

  it('keeps default-theme status tokens on the documented lightness ladder', () => {
    const rootBlock = defaultThemeTokens()
    const lightnessValues = EXPECTED_DEFAULT_LIGHTNESS.map(([token]) =>
      tokenLightness(rootBlock, token),
    )

    expect(lightnessValues).toEqual(EXPECTED_DEFAULT_LIGHTNESS.map(([, value]) => value))
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
