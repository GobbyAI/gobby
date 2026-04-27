import { JsonBlock } from './JsonBlock'

interface UnknownBlockCardProps {
  blockType: string
  raw: Record<string, unknown>
}

export function UnknownBlockCard({ blockType, raw }: UnknownBlockCardProps) {
  return (
    <div className="my-1.5 rounded border border-amber-500/30 bg-amber-500/5 text-xs">
      <details>
        <summary className="cursor-pointer select-none px-3 py-1.5 text-amber-400/80 hover:text-amber-300 font-medium">
          Unknown block: <code className="ml-1 font-mono">{blockType}</code>
        </summary>
        <JsonBlock
          value={raw}
          className="border-t border-amber-500/20 px-3 py-2 text-muted-foreground/70 leading-relaxed"
        />
      </details>
    </div>
  )
}
