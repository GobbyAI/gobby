import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'

import { cn } from '../../lib/utils'
import { useResolvedTheme } from '../../hooks/useResolvedTheme'
import { getCodeBlockTheme } from '../shared/codeBlockTheme'

// Mono overflow contract (.impeccable.md): pre-wrap + overflow-wrap:anywhere.
// `break-word` keeps a long token's full width as the min-content size, so a
// min-width-constrained ancestor lets the block outgrow the viewport and the
// card's overflow-hidden clips it mid-word; `anywhere` wraps instead.
const JSON_BLOCK_CLASS = 'overflow-hidden whitespace-pre-wrap [overflow-wrap:anywhere]'

interface JsonBlockProps {
  value: unknown
  className?: string
  testId?: string
}

function formatJsonValue(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

export function JsonBlock({
  value,
  className,
  testId,
}: JsonBlockProps) {
  const resolvedTheme = useResolvedTheme()
  return (
    <div className={cn(JSON_BLOCK_CLASS, className)}>
      <SyntaxHighlighter
        data-testid={testId}
        style={getCodeBlockTheme(resolvedTheme)}
        language="json"
        PreTag="div"
        wrapLongLines
        customStyle={{
          margin: 0,
          // Sit on the surrounding tool card / message surface rather than
          // the shared code theme's var(--code-bg) slab; the theme-aware
          // syntax palette is unaffected.
          background: 'transparent',
          borderRadius: 'inherit',
          maxHeight: 'inherit',
          overflowY: 'auto',
          overflowX: 'hidden',
          whiteSpace: 'pre-wrap',
          overflowWrap: 'anywhere',
        }}
      >
        {formatJsonValue(value)}
      </SyntaxHighlighter>
    </div>
  )
}
