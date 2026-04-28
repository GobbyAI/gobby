import type { ChatMode, ChatModeInfo } from '../../types/chat'
import { CHAT_MODES } from '../../types/chat'
import { cn } from '../../lib/utils'

interface ModeSelectorProps {
  mode: ChatMode
  onModeChange: (mode: ChatMode) => void
  disabled?: boolean
  modes?: ChatModeInfo[]
}

export function ModeSelector({
  mode,
  onModeChange,
  disabled,
  modes = CHAT_MODES,
}: ModeSelectorProps) {
  return (
    <div className="flex rounded-md border border-border text-xs" role="radiogroup" aria-label="Chat mode">
      {modes.map((m, i) => (
        <button
          key={m.id}
          role="radio"
          aria-checked={m.id === mode}
          className={cn(
            'px-2 py-1 transition-colors',
            i === 0 && 'rounded-l-md',
            i === modes.length - 1 && 'rounded-r-md',
            m.id === mode
              ? 'bg-accent/15 text-accent'
              : 'text-muted-foreground hover:bg-muted'
          )}
          onClick={() => onModeChange(m.id)}
          disabled={disabled}
          title={m.description}
        >
          {m.label}
        </button>
      ))}
    </div>
  )
}
