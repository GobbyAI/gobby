import React, { useCallback, useMemo } from 'react'

import { CodeBlock } from '../shared/CodeBlock'
import { computeLineDiff } from './ToolCallCard.helpers'
import { TOOL_RESULT_CUSTOM_STYLE } from './ToolCallCard.styles'

export function InlineDiff({ oldStr, newStr, language }: { oldStr: string; newStr: string; language: string }) {
  const diff = useMemo(() => computeLineDiff(oldStr, newStr), [oldStr, newStr])
  const content = useMemo(() => diff.map(e => {
    const prefix = e.type === 'add' ? '+' : e.type === 'remove' ? '-' : ' '
    return `${prefix} ${e.line}`
  }).join('\n'), [diff])

  const lineProps = useCallback((lineNumber: number): React.HTMLProps<HTMLElement> => {
    const entry = diff[lineNumber - 1]
    if (!entry) return { style: { display: 'block' } }
    const bg = entry.type === 'add' ? 'color-mix(in srgb, var(--color-success-foreground) 15%, transparent)'
             : entry.type === 'remove' ? 'color-mix(in srgb, var(--color-error) 25%, transparent)'
             : 'transparent'
    return { style: { background: bg, display: 'block' } }
  }, [diff])

  const lineNumberStyleFn = useCallback((lineNumber: number) => {
    const entry = diff[lineNumber - 1]
    const color = entry?.type === 'add' ? 'var(--color-success-foreground)'
               : entry?.type === 'remove' ? 'var(--color-error)'
               : 'var(--text-muted)'
    return { color }
  }, [diff])

  return (
    <CodeBlock
      language={language}
      startingLineNumber={1}
      wrapLines
      lineProps={lineProps}
      lineNumberStyleFn={lineNumberStyleFn}
      customStyle={TOOL_RESULT_CUSTOM_STYLE}
    >
      {content}
    </CodeBlock>
  )
}
