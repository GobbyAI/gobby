import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'

import { cn } from '../../lib/utils'

const JSON_BLOCK_CLASS =
  'overflow-hidden whitespace-pre-wrap font-mono text-xs'

interface JsonBlockProps {
  value: unknown
  className?: string
  breakMode?: 'words' | 'all'
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
  breakMode = 'words',
}: JsonBlockProps) {
  return (
    <div
      className={cn(
        JSON_BLOCK_CLASS,
        breakMode === 'all' ? 'break-all' : 'break-words',
        className,
      )}
    >
      <SyntaxHighlighter
        style={oneDark}
        language="json"
        PreTag="div"
        wrapLongLines
        customStyle={{
          margin: 0,
          background: 'transparent',
          borderRadius: 'inherit',
          maxHeight: 'inherit',
          overflowY: 'auto',
          overflowX: 'hidden',
          whiteSpace: 'pre-wrap',
          overflowWrap: breakMode === 'all' ? 'anywhere' : 'break-word',
        }}
        codeTagProps={{
          style: {
            background: 'transparent',
            fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace",
            whiteSpace: 'pre-wrap',
          },
        }}
      >
        {formatJsonValue(value)}
      </SyntaxHighlighter>
    </div>
  )
}
