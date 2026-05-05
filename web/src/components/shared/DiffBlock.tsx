import React, { useCallback, useMemo } from 'react'

import { CodeBlock } from './CodeBlock'
import { computeLineDiff } from '../chat/ToolCallCard.helpers'

type DiffEntryType = 'add' | 'remove' | 'keep' | 'hunk' | 'meta'

type DiffEntry = { type: DiffEntryType; line: string }

type SyntheticInput = {
  mode: 'synthetic'
  oldStr: string
  newStr: string
  language?: string
}

type UnifiedInput = {
  mode: 'unified'
  diff: string
  path?: string
  onCopy?: () => void
  /**
   * Render the path/copy header bar above the diff. Activity-panel surfaces
   * pass `header` to mirror the previous DiffView chrome; chat-side callers
   * omit it.
   */
  header?: boolean
}

type CommonProps = {
  customStyle?: React.CSSProperties
  className?: string
}

export type DiffBlockProps = CommonProps & (SyntheticInput | UnifiedInput)

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

const UNIFIED_DIFF_STYLE: React.CSSProperties = {
  margin: 0,
  padding: 0,
  fontSize: '0.75rem',
  borderRadius: 0,
  height: '100%',
  overflow: 'auto',
}

function classifyUnifiedLine(line: string): DiffEntryType {
  if (line.startsWith('@@')) return 'hunk'
  if (
    line.startsWith('+++') ||
    line.startsWith('---') ||
    line.startsWith('diff ') ||
    line.startsWith('index ')
  ) {
    return 'meta'
  }
  if (line.startsWith('+')) return 'add'
  if (line.startsWith('-')) return 'remove'
  return 'keep'
}

function tintForType(type: DiffEntryType): string {
  if (type === 'add') {
    return 'color-mix(in srgb, var(--color-success-foreground) 15%, transparent)'
  }
  if (type === 'remove') {
    return 'color-mix(in srgb, var(--color-error) 25%, transparent)'
  }
  if (type === 'hunk') {
    return 'color-mix(in srgb, var(--color-info) 8%, transparent)'
  }
  return 'transparent'
}

function gutterColorForType(type: DiffEntryType): string {
  if (type === 'add') return 'var(--color-success-foreground)'
  if (type === 'remove') return 'var(--color-error)'
  if (type === 'hunk') return 'var(--color-info)'
  return 'var(--text-muted)'
}

/**
 * Shared diff renderer. Two input modes:
 *
 * - `mode="synthetic"` — caller supplies before/after strings; the component
 *   computes a longest-common-subsequence line diff and prefixes each row
 *   with `+ `/`- `/`  `.
 * - `mode="unified"` — caller supplies raw unified-diff text (e.g. from
 *   `git diff`); each row is classified by its leading prefix and rendered
 *   through Prism's `diff` language so meta/hunk lines pick up syntax color.
 *
 * Both modes share the same green/red gutter + per-row background tint via
 * `lineNumberStyleFn` and `lineProps` callbacks on the underlying `CodeBlock`.
 */
export function DiffBlock(props: DiffBlockProps) {
  const computed = useMemo(() => {
    if (props.mode === 'unified') {
      const lines = props.diff.split('\n')
      const entries: DiffEntry[] = lines.map((line) => ({
        type: classifyUnifiedLine(line),
        line,
      }))
      return {
        entries,
        content: lines.join('\n'),
        language: 'diff',
      }
    }
    const raw = computeLineDiff(props.oldStr, props.newStr)
    const entries: DiffEntry[] = raw.map(({ type, line }) => ({ type, line }))
    const content = raw
      .map((e) => {
        const prefix = e.type === 'add' ? '+' : e.type === 'remove' ? '-' : ' '
        return `${prefix} ${e.line}`
      })
      .join('\n')
    return { entries, content, language: props.language ?? 'text' }
  }, [props])

  const lineProps = useCallback(
    (lineNumber: number): React.HTMLProps<HTMLElement> => {
      const entry = computed.entries[lineNumber - 1]
      if (!entry) return { style: { display: 'block' } }
      return {
        style: { background: tintForType(entry.type), display: 'block' },
      }
    },
    [computed],
  )

  const lineNumberStyleFn = useCallback(
    (lineNumber: number) => {
      const entry = computed.entries[lineNumber - 1]
      return {
        color: entry ? gutterColorForType(entry.type) : 'var(--text-muted)',
      }
    },
    [computed],
  )

  const defaultStyle =
    props.mode === 'unified' ? UNIFIED_DIFF_STYLE : TOOL_RESULT_DIFF_STYLE

  const block = (
    <CodeBlock
      language={computed.language}
      startingLineNumber={1}
      wrapLines
      lineProps={lineProps}
      lineNumberStyleFn={lineNumberStyleFn}
      customStyle={props.customStyle ?? defaultStyle}
      className={props.className}
    >
      {computed.content}
    </CodeBlock>
  )

  if (props.mode === 'unified' && props.header) {
    return (
      <div className="flex flex-col h-full">
        <div
          className="flex items-center gap-2 px-3 border-b border-border shrink-0"
          style={{ height: 36, background: 'var(--bg-secondary)' }}
        >
          <span className="text-xs font-mono text-muted-foreground truncate flex-1">
            {props.path}
          </span>
          {props.onCopy && (
            <button
              onClick={props.onCopy}
              className="text-xs text-muted-foreground hover:text-foreground shrink-0"
              title="Copy diff"
            >
              Copy
            </button>
          )}
        </div>
        <div className="flex-1 min-h-0 overflow-auto">{block}</div>
      </div>
    )
  }

  return block
}
