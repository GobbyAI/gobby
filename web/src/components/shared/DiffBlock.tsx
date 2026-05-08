import React, { useCallback, useMemo } from 'react'

import { CodeBlock } from './CodeBlock'
import {
  type DiffLine,
  gutterColorForType,
  tintForType,
} from './DiffBlock.helpers'

export type DiffVariant = 'inline' | 'side-by-side'

export type DiffLineStyleFn = (
  lineNumber: number,
  line: DiffLine,
) => React.CSSProperties

export type DiffBlockProps = {
  lines: DiffLine[]
  language?: string
  variant?: DiffVariant
  /**
   * Override the per-line gutter style. Return value is merged over the
   * default type-based color (success-foreground for adds, error for
   * removes, info for hunks, muted for everything else).
   */
  lineNumberStyleFn?: DiffLineStyleFn
  customStyle?: React.CSSProperties
  className?: string
  /** Activity-panel chrome: render the path/copy header bar. */
  header?: boolean
  path?: string
  onCopy?: () => void
}

const TOOL_RESULT_DIFF_STYLE: React.CSSProperties = {
  margin: 0,
  padding: '0.75rem',
  fontSize: '0.75rem',
  borderRadius: '0.25rem',
  maxHeight: '24rem',
  overflowY: 'auto',
  overflowX: 'hidden',
  whiteSpace: 'pre-wrap',
  overflowWrap: 'anywhere',
}

const FULL_HEIGHT_DIFF_STYLE: React.CSSProperties = {
  margin: 0,
  padding: 0,
  fontSize: '0.75rem',
  borderRadius: 0,
  height: '100%',
  overflow: 'auto',
}

/**
 * Shared diff renderer. Pass a normalized `lines: DiffLine[]` array and
 * a `variant`:
 *
 * - `variant="inline"` (default) — single column, each row tinted by
 *   type via the underlying `CodeBlock`'s `lineProps` callback.
 * - `variant="side-by-side"` — two columns: removes on the left, adds
 *   on the right, keeps mirrored to both. Hunk/meta rows stretch the
 *   full width above each section.
 *
 * Use `computeSyntheticDiffLines` or `parseUnifiedDiffLines` from
 * `./DiffBlock.helpers` to build the `lines` array from the two common
 * input shapes.
 */
export function DiffBlock({
  lines,
  language = 'text',
  variant = 'inline',
  lineNumberStyleFn,
  customStyle,
  className,
  header,
  path,
  onCopy,
}: DiffBlockProps) {
  const content = useMemo(() => lines.map((l) => l.text).join('\n'), [lines])

  const lineProps = useCallback(
    (lineNumber: number): React.HTMLProps<HTMLElement> => {
      const entry = lines[lineNumber - 1]
      if (!entry) return { style: { display: 'block' } }
      return {
        style: { background: tintForType(entry.type), display: 'block' },
      }
    },
    [lines],
  )

  const resolvedLineNumberStyleFn = useCallback(
    (lineNumber: number) => {
      const entry = lines[lineNumber - 1]
      const base: React.CSSProperties = {
        color: entry ? gutterColorForType(entry.type) : 'var(--text-muted)',
      }
      if (lineNumberStyleFn && entry) {
        return { ...base, ...lineNumberStyleFn(lineNumber, entry) }
      }
      return base
    },
    [lines, lineNumberStyleFn],
  )

  const defaultStyle = header ? FULL_HEIGHT_DIFF_STYLE : TOOL_RESULT_DIFF_STYLE
  const resolvedStyle = customStyle ?? defaultStyle

  const inline = (
    <CodeBlock
      language={language}
      startingLineNumber={1}
      wrapLines
      lineProps={lineProps}
      lineNumberStyleFn={resolvedLineNumberStyleFn}
      customStyle={resolvedStyle}
      className={className}
    >
      {content}
    </CodeBlock>
  )

  const body =
    variant === 'side-by-side' ? (
      <SideBySideDiff
        lines={lines}
        language={language}
        customStyle={resolvedStyle}
        className={className}
      />
    ) : (
      inline
    )

  if (header) {
    return (
      <div className="flex flex-col h-full">
        <div
          className="flex items-center gap-2 px-3 border-b border-border shrink-0"
          style={{ height: 36, background: 'var(--bg-secondary)' }}
        >
          <span className="text-xs font-mono text-muted-foreground truncate flex-1">
            {path}
          </span>
          {onCopy && (
            <button
              onClick={onCopy}
              className="text-xs text-muted-foreground hover:text-foreground shrink-0"
              title="Copy diff"
            >
              Copy
            </button>
          )}
        </div>
        <div className="flex-1 min-h-0 overflow-auto">{body}</div>
      </div>
    )
  }

  return body
}

type SideBySideProps = {
  lines: DiffLine[]
  language: string
  customStyle: React.CSSProperties
  className?: string
}

/**
 * Minimal side-by-side variant. Splits lines into a left column
 * (removes + keeps + hunk/meta) and a right column (adds + keeps +
 * hunk/meta), preserving the existing per-row tinting via lineProps.
 * Row alignment relies on each side keeping its own ordering — this
 * is intentionally simpler than a full LCS-aware aligner and is good
 * enough for review at a glance.
 */
function SideBySideDiff({
  lines,
  language,
  customStyle,
  className,
}: SideBySideProps) {
  const oldLines = useMemo(
    () => lines.filter((l) => l.type !== 'add'),
    [lines],
  )
  const newLines = useMemo(
    () => lines.filter((l) => l.type !== 'remove'),
    [lines],
  )

  const sideLineProps = (sideLines: DiffLine[]) =>
    (lineNumber: number): React.HTMLProps<HTMLElement> => {
      const entry = sideLines[lineNumber - 1]
      if (!entry) return { style: { display: 'block' } }
      return {
        style: { background: tintForType(entry.type), display: 'block' },
      }
    }

  const sideLineNumberStyleFn = (sideLines: DiffLine[]) =>
    (lineNumber: number): React.CSSProperties => {
      const entry = sideLines[lineNumber - 1]
      return {
        color: entry ? gutterColorForType(entry.type) : 'var(--text-muted)',
      }
    }

  return (
    <div className={className} style={{ display: 'flex', gap: 0 }}>
      <div style={{ flex: 1, minWidth: 0, borderRight: '1px solid var(--border)' }}>
        <CodeBlock
          language={language}
          startingLineNumber={1}
          wrapLines
          lineProps={sideLineProps(oldLines)}
          lineNumberStyleFn={sideLineNumberStyleFn(oldLines)}
          customStyle={customStyle}
        >
          {oldLines.map((l) => l.text).join('\n')}
        </CodeBlock>
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <CodeBlock
          language={language}
          startingLineNumber={1}
          wrapLines
          lineProps={sideLineProps(newLines)}
          lineNumberStyleFn={sideLineNumberStyleFn(newLines)}
          customStyle={customStyle}
        >
          {newLines.map((l) => l.text).join('\n')}
        </CodeBlock>
      </div>
    </div>
  )
}
