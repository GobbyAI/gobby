import { useEffect, useRef, type RefObject } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

interface UseDialogFocusOptions {
  ref: RefObject<HTMLElement | null>
  isOpen: boolean
  onClose: () => void
  trap?: boolean
}

function isFocusableVisible(el: HTMLElement): boolean {
  if (el.closest('[aria-hidden="true"]')) return false
  if (el.getClientRects().length === 0) return false
  const style = window.getComputedStyle(el)
  return style.display !== 'none' && style.visibility !== 'hidden'
}

export function useDialogFocus({ ref, isOpen, onClose, trap = true }: UseDialogFocusOptions): void {
  const dialogRef = useRef(ref)

  useEffect(() => {
    dialogRef.current = ref
  }, [ref])

  useEffect(() => {
    if (!isOpen) return
    const node = dialogRef.current.current
    if (!node) return

    const previouslyFocused = document.activeElement as HTMLElement | null
    const hadTabIndex = node.hasAttribute('tabindex')
    const previousTabIndex = node.getAttribute('tabindex')
    if (!hadTabIndex) {
      node.setAttribute('tabindex', '-1')
    }

    const focusables = () =>
      Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter(isFocusableVisible)

    const initial = node.querySelector<HTMLElement>('[autofocus]') ?? focusables()[0] ?? node
    requestAnimationFrame(() => {
      if (!node.contains(document.activeElement)) initial.focus()
    })

    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
        return
      }
      if (!trap || e.key !== 'Tab') return
      const items = focusables()
      if (items.length === 0) {
        e.preventDefault()
        node.focus()
        return
      }
      const first = items[0]
      const last = items[items.length - 1]
      const active = document.activeElement as HTMLElement | null
      if (e.shiftKey && (active === first || !node.contains(active))) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }

    node.addEventListener('keydown', handleKey)
    return () => {
      node.removeEventListener('keydown', handleKey)
      if (hadTabIndex && previousTabIndex !== null) {
        node.setAttribute('tabindex', previousTabIndex)
      } else {
        node.removeAttribute('tabindex')
      }
      if (previouslyFocused && document.contains(previouslyFocused)) {
        previouslyFocused.focus()
      }
    }
  }, [isOpen, onClose, trap])
}
