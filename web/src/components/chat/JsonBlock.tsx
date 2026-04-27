import { cn } from '../../lib/utils'

const JSON_BLOCK_PRE_CLASS =
  'overflow-y-auto overflow-x-hidden whitespace-pre-wrap font-mono text-xs'

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
    <pre
      className={cn(
        JSON_BLOCK_PRE_CLASS,
        breakMode === 'all' ? 'break-all' : 'break-words',
        className,
      )}
    >
      {formatJsonValue(value)}
    </pre>
  )
}
