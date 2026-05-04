import { describe, expect, it } from 'vitest'
import { render } from '@testing-library/react'

import { InlineDiff } from '../ToolCallCard.diff'

/**
 * InlineDiff colors changed lines via two paths:
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

describe('InlineDiff', () => {
  it('tints add/remove rows with translucent green/red backgrounds via lineProps', () => {
    const oldStr = 'alpha\nbeta\ngamma\n'
    const newStr = 'alpha\nBETA\ngamma\ndelta\n'

    const { container } = render(
      <InlineDiff oldStr={oldStr} newStr={newStr} language="text" />,
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
      <InlineDiff oldStr={oldStr} newStr={newStr} language="text" />,
    )

    expect(container.firstChild).toMatchSnapshot()
  })
})
