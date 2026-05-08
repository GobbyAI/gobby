import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import { DiffBlock } from '../DiffBlock'
import {
  computeSyntheticDiffLines,
  parseUnifiedDiffLines,
} from '../DiffBlock.helpers'

/**
 * DiffBlock colors changed lines via two paths:
 *   1. `lineProps` sets a translucent green/red BACKGROUND on the row
 *      (this is the visible diff highlight).
 *   2. `lineNumberStyleFn` returns per-line text color tokens for the
 *      gutter; react-syntax-highlighter's class-based stylesheet merge
 *      always wins over the inline element style for `color`, so the
 *      gutter renders in the theme's comment color and the user-provided
 *      color is effectively decorative. The callback API is preserved
 *      for non-color customization (font-weight, opacity, etc.).
 *
 * The user-visible "green +/red -" effect on diffs is the row background.
 */

describe('DiffBlock — inline variant from synthetic lines', () => {
  it('tints add/remove rows with translucent green/red backgrounds via lineProps', () => {
    const oldStr = 'alpha\nbeta\ngamma\n'
    const newStr = 'alpha\nBETA\ngamma\ndelta\n'

    const lines = computeSyntheticDiffLines(oldStr, newStr)
    const { container } = render(
      <DiffBlock lines={lines} language="text" />,
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
      <DiffBlock
        lines={computeSyntheticDiffLines(oldStr, newStr)}
        language="text"
      />,
    )

    expect(container.firstChild).toMatchSnapshot()
  })
})

describe('DiffBlock — inline variant from parsed unified diff', () => {
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
    const lines = parseUnifiedDiffLines(sampleDiff)
    const { container } = render(
      <DiffBlock lines={lines} language="diff" />,
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
        lines={parseUnifiedDiffLines(sampleDiff)}
        language="diff"
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
    render(
      <DiffBlock
        lines={parseUnifiedDiffLines(sampleDiff)}
        language="diff"
        path="src/foo.ts"
      />,
    )

    expect(screen.queryByText('src/foo.ts')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy' })).toBeNull()
  })

  it('caller-provided lineNumberStyleFn merges over the type-based default', () => {
    const lines = parseUnifiedDiffLines(sampleDiff)
    const calls: Array<{ lineNumber: number; type: string }> = []
    const styleFn = (lineNumber: number, line: { type: string }) => {
      calls.push({ lineNumber, type: line.type })
      return { fontWeight: 'bold' as const }
    }

    render(
      <DiffBlock lines={lines} language="diff" lineNumberStyleFn={styleFn} />,
    )

    expect(calls.length).toBeGreaterThan(0)
    expect(calls[0]).toMatchObject({ type: expect.any(String) })
  })
})

describe('DiffBlock — side-by-side variant', () => {
  it('renders two columns when variant=side-by-side', () => {
    const oldStr = 'alpha\nbeta\n'
    const newStr = 'alpha\nBETA\n'
    const lines = computeSyntheticDiffLines(oldStr, newStr)

    const { container } = render(
      <DiffBlock lines={lines} language="text" variant="side-by-side" />,
    )

    const flexRoot = container.querySelector('[style*="display: flex"]')
    expect(flexRoot).not.toBeNull()
    expect(flexRoot?.children.length).toBe(2)
  })
})

describe('parseUnifiedDiffLines', () => {
  it('tracks old/new line numbers across hunk boundaries', () => {
    const diff = [
      '@@ -10,3 +20,4 @@',
      ' keep1',
      '-removed',
      '+added1',
      '+added2',
      ' keep2',
    ].join('\n')

    const lines = parseUnifiedDiffLines(diff)
    expect(lines.map((l) => l.type)).toEqual([
      'hunk',
      'keep',
      'remove',
      'add',
      'add',
      'keep',
    ])
    expect(lines[1]).toMatchObject({ oldLineNumber: 10, newLineNumber: 20 })
    expect(lines[2]).toMatchObject({ oldLineNumber: 11 })
    expect(lines[2].newLineNumber).toBeUndefined()
    expect(lines[3]).toMatchObject({ newLineNumber: 21 })
    expect(lines[3].oldLineNumber).toBeUndefined()
    expect(lines[4]).toMatchObject({ newLineNumber: 22 })
    expect(lines[4].oldLineNumber).toBeUndefined()
    expect(lines[5]).toMatchObject({ oldLineNumber: 12, newLineNumber: 23 })
  })
})
