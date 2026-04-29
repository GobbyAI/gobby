import type { ChatMode, ChatModeInfo } from '../../types/chat'
import { CHAT_MODES } from '../../types/chat'
import { SegmentedControl } from '../ui/SegmentedControl'

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
    <SegmentedControl<ChatMode>
      value={mode}
      onChange={onModeChange}
      options={modes.map((m) => ({
        value: m.id,
        label: m.label,
        title: m.description,
      }))}
      ariaLabel="Chat mode"
      disabled={disabled}
    />
  )
}
