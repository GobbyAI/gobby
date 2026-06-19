import type { PointerEventHandler, RefObject } from 'react'
import { RecordIcon, SendIcon, StopIcon } from './ChatInputIcons'
import type { ChatInputPrimaryButtonKind } from './useChatInputPrimaryAction'

interface ChatInputPrimaryButtonProps {
  buttonRef: RefObject<HTMLButtonElement>
  className: string
  disabled: boolean
  kind: ChatInputPrimaryButtonKind
  label: string
  onClick: () => void
  onMicPointerCancel: PointerEventHandler<HTMLButtonElement>
  onMicPointerDown: PointerEventHandler<HTMLButtonElement>
  onMicPointerMove: PointerEventHandler<HTMLButtonElement>
  onMicPointerUp: PointerEventHandler<HTMLButtonElement>
}

function isMicButton(kind: ChatInputPrimaryButtonKind): boolean {
  return kind === 'mic-idle' || kind === 'mic-recording'
}

export function ChatInputPrimaryButton({
  buttonRef,
  className,
  disabled,
  kind,
  label,
  onClick,
  onMicPointerCancel,
  onMicPointerDown,
  onMicPointerMove,
  onMicPointerUp,
}: ChatInputPrimaryButtonProps) {
  const micButton = isMicButton(kind)

  return (
    <button
      ref={buttonRef}
      type="button"
      className={className}
      onClick={micButton ? undefined : onClick}
      onPointerDown={micButton ? onMicPointerDown : undefined}
      onPointerUp={micButton ? onMicPointerUp : undefined}
      onPointerMove={micButton ? onMicPointerMove : undefined}
      onPointerCancel={micButton ? onMicPointerCancel : undefined}
      title={label}
      aria-label={label}
      aria-pressed={kind === 'mic-recording' ? true : undefined}
      disabled={disabled}
    >
      {kind === 'stop' ? <StopIcon /> : kind === 'send' ? <SendIcon /> : <RecordIcon />}
    </button>
  )
}
