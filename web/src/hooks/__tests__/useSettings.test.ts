import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cacheBustedIconHref, useSettings } from '../useSettings'

function iconLink() {
  return document.head.querySelector<HTMLLinkElement>('link[rel="icon"]')
}

function appleTouchIconLink() {
  return document.head.querySelector<HTMLLinkElement>('link[rel="apple-touch-icon"]')
}

describe('useSettings', () => {
  beforeEach(() => {
    localStorage.clear()
    document.head.innerHTML = `
      <link rel="icon" type="image/png" href="/logo.png?v=2">
      <link rel="apple-touch-icon" href="/logo.png?v=2">
    `
    document.documentElement.removeAttribute('data-theme')
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
  })

  afterEach(() => {
    document.head.innerHTML = ''
    document.documentElement.removeAttribute('data-theme')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('applies the light theme and light logo icons from persisted settings', async () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ theme: 'light' }))

    renderHook(() => useSettings())

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    })
    expect(iconLink()).toHaveAttribute('href', '/logo-light.png?v=2')
    expect(iconLink()).toHaveAttribute('type', 'image/png')
    expect(appleTouchIconLink()).toHaveAttribute('href', '/logo-light.png?v=2')
  })

  it('updates document theme and icon links when theme changes', async () => {
    const { result } = renderHook(() => useSettings())

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    })
    expect(iconLink()).toHaveAttribute('href', '/logo.png?v=2')
    expect(appleTouchIconLink()).toHaveAttribute('href', '/logo.png?v=2')

    act(() => {
      result.current.updateTheme('light')
    })

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    })
    expect(iconLink()).toHaveAttribute('href', '/logo-light.png?v=2')
    expect(appleTouchIconLink()).toHaveAttribute('href', '/logo-light.png?v=2')
  })

  it('replaces the icon cache param while preserving other query params and fragments', () => {
    expect(cacheBustedIconHref('/logo.png?theme=dark&v=1#mask')).toBe(
      '/logo.png?theme=dark&v=2#mask',
    )
  })
})
