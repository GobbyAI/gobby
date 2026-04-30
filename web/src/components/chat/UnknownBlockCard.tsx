import { JsonBlock } from './JsonBlock'

interface UnknownBlockCardProps {
  blockType: string
  raw: Record<string, unknown>
}

export function UnknownBlockCard({ blockType, raw }: UnknownBlockCardProps) {
  return (
    <div className="my-1.5 rounded border border-warning-foreground/30 bg-warning-foreground/5 text-xs">
      <details>
        <summary className="cursor-pointer select-none px-3 py-1.5 text-warning-foreground/80 hover:text-warning-foreground font-medium">
          Unknown block: <code className="ml-1 font-mono">{blockType}</code>
        </summary>
        <JsonBlock
          value={raw}
          className="border-t border-warning-foreground/20 px-3 py-2 text-muted-foreground/70 leading-relaxed"
        />
      </details>
    </div>
  )
}
