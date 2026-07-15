import type { ReactElement, RefObject } from 'react'
import type { PaletteItem } from '../../hooks/useColonAutocomplete'
import { cn } from '../../lib/utils'

interface ChatCommandPaletteProps {
  items: PaletteItem[]
  selectedIndex: number
  onSelect: (item: PaletteItem) => void
  paletteRef: RefObject<HTMLDivElement>
  listboxId: string
  optionIdPrefix: string
}

export function ChatCommandPalette({
  items,
  selectedIndex,
  onSelect,
  paletteRef,
  listboxId,
  optionIdPrefix,
}: ChatCommandPaletteProps): ReactElement {
  return (
    <div
      ref={paletteRef}
      id={listboxId}
      className="command-palette font-sans"
      role="listbox"
      aria-label="Chat commands"
    >
      {items.map((item, index) => (
        <button
          key={
            item.kind === 'command'
              ? item.name
              : `${item.parentCommand}:${item.name}${item.serverName ? `:${item.serverName}` : ''}`
          }
          id={`${optionIdPrefix}-${index}`}
          className={cn(
            'block w-full px-3 py-2 text-left text-sm cursor-pointer',
            index === selectedIndex
              ? 'bg-accent/20 text-foreground'
              : 'text-muted-foreground hover:bg-muted',
          )}
          type="button"
          role="option"
          aria-selected={index === selectedIndex}
          tabIndex={-1}
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
                <span className="ml-1.5 text-[length:var(--text-2xs)] px-1 py-0.5 rounded bg-accent/10 text-accent">
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
