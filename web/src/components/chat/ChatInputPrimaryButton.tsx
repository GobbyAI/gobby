import type { KeyboardEventHandler, PointerEventHandler, RefObject } from 'react'
import { RecordIcon, SendIcon, StopIcon } from './ChatInputIcons'
import type { ChatInputPrimaryButtonKind } from './useChatInputPrimaryAction'

interface ChatInputPrimaryButtonProps {
  buttonRef: RefObject<HTMLButtonElement>
  className: string
  disabled: boolean
  kind: ChatInputPrimaryButtonKind
  label: string
  onClick: () => void
  onMicKeyDown: KeyboardEventHandler<HTMLButtonElement>
  onMicKeyUp: KeyboardEventHandler<HTMLButtonElement>
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
  onMicKeyDown,
  onMicKeyUp,
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
      onClick={onClick}
      onKeyDown={micButton ? onMicKeyDown : undefined}
      onKeyUp={micButton ? onMicKeyUp : undefined}
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
