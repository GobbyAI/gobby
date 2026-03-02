import { useMemo, useState } from 'react'
import { Markdown } from './Markdown'

interface ThinkingBlockProps {
  content: string
  messageId: string
}

/** Convert single newlines to markdown line breaks (two trailing spaces) */
function preserveLineBreaks(text: string): string {
  return text.replace(/\n/g, '  \n')
}

export function ThinkingBlock({ content, messageId }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false)
  const processed = useMemo(() => preserveLineBreaks(content), [content])

  return (
    <div
      className="my-2 rounded-lg border border-border bg-muted/30"
    >
      <div
        className="flex items-center gap-2 px-3 py-2 text-sm text-muted-foreground cursor-pointer"
        tabIndex={0}
        role="button"
        aria-expanded={expanded}
        onClick={() => setExpanded(!expanded)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setExpanded(!expanded)
          }
        }}
      >
        <span className="text-xs">{expanded ? '\u25BC' : '\u25B6'}</span>
        <span className="font-medium">Thinking</span>
      </div>
      {expanded && content && (
        <div
          className="px-3 pb-3 text-sm text-muted-foreground prose-sm"
          onClick={(e) => e.stopPropagation()}
        >
          <Markdown content={processed} id={`${messageId}-thinking`} />
        </div>
      )}
    </div>
  )
}
