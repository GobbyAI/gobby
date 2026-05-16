import { useSyncExternalStore } from 'react'

/**
 * The active resolved app theme, read from the `data-theme` attribute that
 * `useSettings` writes to `<html>` (it resolves `system` to the OS scheme
 * before setting it, so this is always a concrete `light` | `dark`).
 *
 * Use for JS-side theming that CSS custom properties can't cover — e.g. the
 * Prism style object passed to react-syntax-highlighter, which is a plain
 * JS object and cannot read `var(--…)` for its token palette.
 */
function read(): 'light' | 'dark' {
  return document.documentElement.getAttribute('data-theme') === 'light'
    ? 'light'
    : 'dark'
}

function subscribe(onChange: () => void): () => void {
  const observer = new MutationObserver(onChange)
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  })
  return () => observer.disconnect()
}

export function useResolvedTheme(): 'light' | 'dark' {
  return useSyncExternalStore(subscribe, read, () => 'dark')
}
