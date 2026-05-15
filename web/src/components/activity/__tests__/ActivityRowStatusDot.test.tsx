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

// Each kind must paint a different lucide icon. The class name carries the
// icon identity (e.g. `lucide-circle-check-big`), which is what we assert
// on — recolouring to gray cannot collapse two kinds onto the same shape.
const ICON_CLASS: Record<StatusKind, string> = {
  success: 'lucide-circle-check-big',
  info: 'lucide-circle-dot',
  warning: 'lucide-triangle-alert',
  error: 'lucide-circle-x',
  paused: 'lucide-pause',
  stopped: 'lucide-octagon',
  disabled: 'lucide-minus',
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

  it('paints a distinct lucide glyph for each kind', () => {
    const seenClasses = new Set<string>()
    for (const kind of KINDS) {
      const span = renderDot(kind)
      const svg = span.querySelector('svg')
      if (!svg) throw new Error(`No icon rendered for kind=${kind}`)
      const cls = svg.getAttribute('class') ?? ''
      expect(cls).toContain(ICON_CLASS[kind])
      seenClasses.add(ICON_CLASS[kind])
    }
    // Each kind gets a distinct lucide icon; uniqueness is the guard
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
// markup — different lucide icon class, different inline color token —
// even when the hue channel is collapsed by the CSS filter. The snapshot
// pins the structure so any future change that re-introduces hue-only
// differentiation (two kinds rendering the same icon) will fail the
// snapshot.
// ----------------------------------------------------------------------
describe('ActivityRowStatusDot — grayscale(1) structural snapshot', () => {
  function grayscaleSnapshot(kind: StatusKind): string {
    const { container } = render(
      <div style={{ filter: 'grayscale(1)' }}>
        <ActivityRowStatusDot kind={kind} label={kind} />
      </div>,
    )
    return container.innerHTML
  }

  it('produces a distinct, stable structure for success', () => {
    expect(grayscaleSnapshot('success')).toMatchInlineSnapshot(
      `"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--color-success-foreground);" data-kind="success" aria-label="success" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-circle-check-big" aria-hidden="true" focusable="false"><path d="M21.801 10A10 10 0 1 1 17 3.335"></path><path d="m9 11 3 3L22 4"></path></svg></span></div>"`,
    )
  })

  it('produces a distinct, stable structure for info', () => {
    expect(grayscaleSnapshot('info')).toMatchInlineSnapshot(
      `"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--color-info);" data-kind="info" aria-label="info" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-circle-dot" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="10"></circle><circle cx="12" cy="12" r="1"></circle></svg></span></div>"`,
    )
  })

  it('produces a distinct, stable structure for warning', () => {
    expect(grayscaleSnapshot('warning')).toMatchInlineSnapshot(
      `"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--color-warning-foreground);" data-kind="warning" aria-label="warning" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-triangle-alert" aria-hidden="true" focusable="false"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"></path><path d="M12 9v4"></path><path d="M12 17h.01"></path></svg></span></div>"`,
    )
  })

  it('produces a distinct, stable structure for error', () => {
    expect(grayscaleSnapshot('error')).toMatchInlineSnapshot(
      `"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--color-error);" data-kind="error" aria-label="error" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-circle-x" aria-hidden="true" focusable="false"><circle cx="12" cy="12" r="10"></circle><path d="m15 9-6 6"></path><path d="m9 9 6 6"></path></svg></span></div>"`,
    )
  })

  it('produces a distinct, stable structure for paused', () => {
    expect(grayscaleSnapshot('paused')).toMatchInlineSnapshot(`"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--text-secondary);" data-kind="paused" aria-label="paused" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-pause" aria-hidden="true" focusable="false"><rect x="14" y="3" width="5" height="18" rx="1"></rect><rect x="5" y="3" width="5" height="18" rx="1"></rect></svg></span></div>"`)
  })

  it('produces a distinct, stable structure for stopped', () => {
    expect(grayscaleSnapshot('stopped')).toMatchInlineSnapshot(`"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--color-inactive);" data-kind="stopped" aria-label="stopped" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-octagon" aria-hidden="true" focusable="false"><path d="M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z"></path></svg></span></div>"`)
  })

  it('produces a distinct, stable structure for disabled', () => {
    expect(grayscaleSnapshot('disabled')).toMatchInlineSnapshot(
      `"<div style="filter: grayscale(1);"><span class="activity-row-status-dot" style="color: var(--text-muted);" data-kind="disabled" aria-label="disabled" role="img" aria-hidden="false"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-minus" aria-hidden="true" focusable="false"><path d="M5 12h14"></path></svg></span></div>"`,
    )
  })
})
