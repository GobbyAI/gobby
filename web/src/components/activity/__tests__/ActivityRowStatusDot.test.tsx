import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'
import {
  ActivityGlyph,
  ActivityRowStatusDot,
  LockGlyph,
  type StatusKind,
} from '../ActivityRowStatusDot'

// The dishonest icons this override exists to retire.
const WARNING_TRIANGLE_D = 'm21.73 18-8-14a2 2 0 0 0-3.48 0'

const KINDS: StatusKind[] = [
  'active',
  'success',
  'info',
  'warning',
  'error',
  'paused',
  'stopped',
  'disabled',
]

const TOKENS: Record<StatusKind, string> = {
  active: 'var(--accent)',
  success: 'var(--color-success-foreground)',
  info: 'var(--color-info)',
  warning: 'var(--color-warning-foreground)',
  error: 'var(--color-error)',
  paused: 'var(--text-secondary)',
  stopped: 'var(--color-inactive)',
  disabled: 'var(--text-muted)',
}

const EXPECTED_DEFAULT_LIGHTNESS: Array<[string, number]> = [
  ['--accent', 82],
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
  active: 'activity-row-status-dot__glyph--active',
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

  it('renders active as an accent play glyph that can pulse', () => {
    const span = renderDot('active', { pulse: true, label: 'Session active' })
    const svg = span.querySelector('svg')
    const polygon = svg?.querySelector('polygon')

    expect(span.getAttribute('data-kind')).toBe('active')
    expect(span.style.color).toBe('var(--accent)')
    expect(span.getAttribute('class')).toContain(
      'activity-row-status-dot--pulse',
    )
    expect(svg?.getAttribute('class')).toContain(
      'activity-row-status-dot__glyph--active',
    )
    expect(polygon?.getAttribute('points')).toBe('6 3 20 12 6 21 6 3')
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

describe('ActivityRowStatusDot — optional glyph override (#14769 / D2)', () => {
  it('renders the kind glyph when no override is supplied', () => {
    const span = renderDot('warning')
    const svg = span.querySelector('svg')
    expect(svg?.getAttribute('data-glyph')).toBeNull()
    expect(svg?.getAttribute('class')).toContain(
      'activity-row-status-dot__glyph--warning',
    )
  })

  it('swaps the shape but keeps the kind color/lightness band', () => {
    const { container } = render(
      <ActivityRowStatusDot kind="warning" glyph={ActivityGlyph} label="Working" />,
    )
    const span = container.querySelector(
      'span.activity-row-status-dot',
    ) as HTMLSpanElement
    const svg = span.querySelector('svg')

    // Color/lightness still derive from `kind` — grayscale ranking preserved.
    expect(span.getAttribute('data-kind')).toBe('warning')
    expect(span.style.color).toBe('var(--color-warning-foreground)')

    // Shape is the honest activity glyph, not the caution triangle.
    expect(svg?.getAttribute('data-glyph')).toBe('activity')
    const paths = Array.from(svg?.querySelectorAll('path') ?? [])
    expect(
      paths.some((p) => (p.getAttribute('d') ?? '').startsWith(WARNING_TRIANGLE_D)),
    ).toBe(false)
  })

  it('renders blocked as a lock, never the error X, while keeping error lightness', () => {
    const { container } = render(
      <ActivityRowStatusDot kind="error" glyph={LockGlyph} label="Blocked" />,
    )
    const span = container.querySelector(
      'span.activity-row-status-dot',
    ) as HTMLSpanElement
    const svg = span.querySelector('svg')

    expect(span.getAttribute('data-kind')).toBe('error')
    expect(span.style.color).toBe('var(--color-error)')
    expect(svg?.getAttribute('data-glyph')).toBe('lock')
    // A lock has a rect body; the error X is two crossed paths in a circle.
    expect(svg?.querySelector('rect')).not.toBeNull()
    expect(svg?.querySelector('circle')).toBeNull()
  })
})
