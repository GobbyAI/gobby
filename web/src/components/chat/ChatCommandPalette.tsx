import type { ReactElement, RefObject } from 'react'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import { cn } from '../../lib/utils'

interface ChatCommandPaletteProps {
  items: PaletteItem[]
  selectedIndex: number
  onSelect: (item: PaletteItem) => void
  paletteRef: RefObject<HTMLDivElement>
}

export function ChatCommandPalette({
  items,
  selectedIndex,
  onSelect,
  paletteRef,
}: ChatCommandPaletteProps): ReactElement {
  return (
    <div ref={paletteRef} className="command-palette font-sans">
      {items.map((item, index) => (
        <button
          key={
            item.kind === 'command'
              ? item.name
              : `${item.parentCommand}:${item.name}${item.serverName ? `:${item.serverName}` : ''}`
          }
          className={cn(
            'block w-full px-3 py-2 text-left text-sm cursor-pointer',
            index === selectedIndex
              ? 'bg-accent/20 text-foreground'
              : 'text-muted-foreground hover:bg-muted',
          )}
          type="button"
          onClick={() => onSelect(item)}
        >
          {item.kind === 'command' ? (
            <>
              <span className="font-mono">/{item.name}</span>
              {item.description && (
                <span className="ml-2 text-xs opacity-60">{item.description}</span>
              )}
              {item.inputHint && (
                <span className="ml-2 font-mono text-xs opacity-70">
                  {item.inputHint}
                </span>
              )}
            </>
          ) : (
            <>
              <span className="font-mono">{item.name}</span>
              {item.serverName && (
                <span className="ml-1.5 text-[10px] px-1 py-0.5 rounded bg-accent/10 text-accent">
                  {item.serverName}
                </span>
              )}
              {item.description && (
                <span className="ml-2 text-xs opacity-60 truncate">{item.description}</span>
              )}
            </>
          )}
        </button>
      ))}
    </div>
  )
}
