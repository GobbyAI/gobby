import React, { useEffect, useRef, useState } from 'react'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'

import { useResolvedTheme } from '../../hooks/useResolvedTheme'
import {
  CODE_CHROME_TYPOGRAPHY,
  CODE_CHROME_VARS,
  getCodeBlockTheme,
  lineNumberStyle as DEFAULT_LINE_NUMBER_STYLE,
} from './codeBlockTheme'

type LineNumberStyle = React.CSSProperties

export interface CodeBlockProps {
  language: string
  children: string
  showLineNumbers?: boolean
  startingLineNumber?: number
  /**
   * Override the canonical 2.5em min-width on the line-number gutter.
   * Use '3em' for 4-digit line numbers (file viewers).
   */
  lineNumberMinWidth?: string
  /**
   * Per-line style override. When provided, the callback's return value
   * is merged over the base line-number style (so the diff renderer can
   * tint lines green/red without re-specifying the gutter geometry).
   */
  lineNumberStyleFn?: (lineNumber: number) => LineNumberStyle
  /**
   * Per-line wrapper props. Used by DiffBlock to set full-line
   * background tints for added/removed rows.
   */
  lineProps?: (lineNumber: number) => React.HTMLProps<HTMLElement>
  wrapLines?: boolean
  wrapLongLines?: boolean
  customStyle?: React.CSSProperties
  /**
   * Per-element style override for the inner `<code>` tag. Mirrors
   * `react-syntax-highlighter`'s `codeTagProps`.
   */
  codeTagProps?: React.HTMLProps<HTMLElement>
  /**
   * Defer SyntaxHighlighter mount until the block scrolls into view.
   * Used by chat-side code blocks for off-screen performance.
   */
  lazy?: boolean
  className?: string
}

/**
 * Shared view-only code block. Wraps `react-syntax-highlighter`'s Prism
 * with the canonical `codeBlockTheme` and `lineNumberStyle` from
 * `codeBlockTheme.ts`.
 *
 * Editing surfaces (artifacts, FilesTab edit mode) keep CodeMirror —
 * this component is for display only.
 */
export function CodeBlock({
  language,
  children,
  showLineNumbers = true,
  startingLineNumber = 1,
  lineNumberMinWidth,
  lineNumberStyleFn,
  lineProps,
  wrapLines,
  wrapLongLines,
  customStyle,
  codeTagProps,
  lazy = false,
  className,
}: CodeBlockProps) {
  const [visible, setVisible] = useState(!lazy)
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!lazy) return
    const el = containerRef.current
    if (!el) return
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true)
          observer.disconnect()
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [lazy])

  const baseLineNumberStyle: LineNumberStyle = {
    ...DEFAULT_LINE_NUMBER_STYLE,
    ...(lineNumberMinWidth ? { minWidth: lineNumberMinWidth } : {}),
  }
  const resolvedTheme = useResolvedTheme()
  const resolvedLineNumberStyle = lineNumberStyleFn
    ? (n: number) => ({ ...baseLineNumberStyle, ...lineNumberStyleFn(n) })
    : baseLineNumberStyle

  if (lazy && !visible) {
    return (
      <div ref={containerRef} className={className}>
        <pre
          style={{
            background: CODE_CHROME_VARS.bg,
            margin: 0,
            padding: CODE_CHROME_TYPOGRAPHY.padding,
            fontSize: CODE_CHROME_TYPOGRAPHY.fontSize,
            fontFamily: CODE_CHROME_TYPOGRAPHY.fontFamily,
            color: 'var(--text-primary)',
            overflow: 'auto',
            borderRadius: CODE_CHROME_TYPOGRAPHY.borderRadius,
            ...(customStyle ?? {}),
          }}
        >
          <code>{children}</code>
        </pre>
      </div>
    )
  }

  const wrapper = (
    <SyntaxHighlighter
      style={getCodeBlockTheme(resolvedTheme)}
      language={language || 'text'}
      PreTag="div"
      showLineNumbers={showLineNumbers}
      startingLineNumber={startingLineNumber}
      lineNumberStyle={resolvedLineNumberStyle}
      lineProps={lineProps}
      wrapLines={wrapLines}
      wrapLongLines={wrapLongLines}
      customStyle={customStyle}
      codeTagProps={codeTagProps}
    >
      {children}
    </SyntaxHighlighter>
  )

  return lazy ? (
    <div ref={containerRef} className={className}>
      {wrapper}
    </div>
  ) : className ? (
    <div className={className}>{wrapper}</div>
  ) : (
    wrapper
  )
}
