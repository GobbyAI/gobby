import type { KeyboardEvent } from 'react'

export function activateOnKeyboard(
  event: KeyboardEvent<HTMLElement>,
  activate: () => void,
): void {
  if (event.target !== event.currentTarget) return
  if (event.key !== 'Enter' && event.key !== ' ') return

  event.preventDefault()
  activate()
}
