import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { DiffBlock } from '../DiffBlock'

/**
 * DiffBlock colors changed lines via two paths:
 *   1. `lineProps` sets a translucent green/red BACKGROUND on the row
 *      (this is the visible diff highlight).
 *   2. `lineNumberStyleFn` returns per-line text color tokens for the
 *      gutter; react-syntax-highlighter's class-based stylesheet merge
 *      (`comment` -> `linenumber` -> `react-syntax-highlighter-line-number`)
 *      always wins over the inline element style for `color`, so the
 *      gutter renders in the theme's comment color and the user-provided
 *      color is effectively decorative. The callback API is preserved
 *      for non-color customization (font-weight, opacity, etc.).
 *
 * The user-visible "green +/red -" effect on diffs is the row background.
 */

describe('DiffBlock — synthetic mode', () => {
  it('tints add/remove rows with translucent green/red backgrounds via lineProps', () => {
    const oldStr = 'alpha\nbeta\ngamma\n'
    const newStr = 'alpha\nBETA\ngamma\ndelta\n'

    const { container } = render(
      <DiffBlock mode="synthetic" oldStr={oldStr} newStr={newStr} language="text" />,
    )

    const rowSpans = Array.from(
      container.querySelectorAll('span[style*="display: block"]'),
    )
    expect(rowSpans.length).toBeGreaterThan(0)

    const rawStyles = rowSpans.map((el) => el.getAttribute('style') ?? '')

    expect(
      rawStyles.some((s) =>
        s.includes('color-mix(in srgb, var(--color-success-foreground)'),
      ),
    ).toBe(true)
    expect(
      rawStyles.some((s) =>
        s.includes('color-mix(in srgb, var(--color-error)'),
      ),
    ).toBe(true)
  })

  it('snapshot of standard add+remove diff stays stable', () => {
    const oldStr = 'alpha\nbeta\ngamma\n'
    const newStr = 'alpha\nBETA\ngamma\ndelta\n'

    const { container } = render(
      <DiffBlock mode="synthetic" oldStr={oldStr} newStr={newStr} language="text" />,
    )

    expect(container.firstChild).toMatchSnapshot()
  })
})

describe('DiffBlock — unified mode', () => {
  const sampleDiff = [
    'diff --git a/src/foo.ts b/src/foo.ts',
    'index 1234..5678 100644',
    '--- a/src/foo.ts',
    '+++ b/src/foo.ts',
    '@@ -1,3 +1,4 @@',
    ' const a = 1',
    '-const b = 2',
    '+const b = 20',
    '+const c = 3',
    ' const d = 4',
  ].join('\n')

  it('classifies +/-/@@/meta lines and tints them via lineProps', () => {
    const { container } = render(
      <DiffBlock mode="unified" diff={sampleDiff} />,
    )

    const rowSpans = Array.from(
      container.querySelectorAll('span[style*="display: block"]'),
    )
    const rawStyles = rowSpans.map((el) => el.getAttribute('style') ?? '')

    expect(
      rawStyles.some((s) =>
        s.includes('color-mix(in srgb, var(--color-success-foreground)'),
      ),
    ).toBe(true)
    expect(
      rawStyles.some((s) =>
        s.includes('color-mix(in srgb, var(--color-error)'),
      ),
    ).toBe(true)
    expect(
      rawStyles.some((s) =>
        s.includes('color-mix(in srgb, var(--color-info)'),
      ),
    ).toBe(true)
  })

  it('renders the path/copy header when `header` is set and wires onCopy', () => {
    const onCopy = vi.fn()

    render(
      <DiffBlock
        mode="unified"
        diff={sampleDiff}
        path="src/foo.ts"
        header
        onCopy={onCopy}
      />,
    )

    expect(screen.getByText('src/foo.ts')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(onCopy).toHaveBeenCalledTimes(1)
  })

  it('omits the header when `header` is not set', () => {
    render(<DiffBlock mode="unified" diff={sampleDiff} path="src/foo.ts" />)

    expect(screen.queryByText('src/foo.ts')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull()
  })
})
