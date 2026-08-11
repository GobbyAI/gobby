import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { configurationClient } from '../../api/config'
import { cacheBustedIconHref, useSettings } from '../useSettings'

function iconLink() {
  return document.head.querySelector<HTMLLinkElement>('link[rel="icon"]')
}

function appleTouchIconLink() {
  return document.head.querySelector<HTMLLinkElement>('link[rel="apple-touch-icon"]')
}

describe('useSettings', () => {
  beforeEach(() => {
    configurationClient.reset()
    localStorage.clear()
    document.head.innerHTML = `
      <link rel="icon" type="image/png" href="/logo.png?v=2">
      <link rel="apple-touch-icon" href="/logo.png?v=2">
    `
    document.documentElement.removeAttribute('data-theme')
    document.documentElement.removeAttribute('data-density')
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false })))
  })

  afterEach(() => {
    document.head.innerHTML = ''
    document.documentElement.removeAttribute('data-theme')
    document.documentElement.removeAttribute('data-density')
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

  it('normalizes and persists plan pending variant', async () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ planPendingVariant: 'bad-value' }))
    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.planPendingVariant).toBe('info')

    act(() => {
      result.current.updatePlanPendingVariant('amber')
    })

    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem('gobby-settings') ?? '{}')
      expect(stored.planPendingVariant).toBe('amber')
    })
  })

  it('persists_settings_through_config_patch', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        return new Response(JSON.stringify({
          committed: true,
          revision: 5,
          changed_keys: ['ui_settings.theme'],
          apply_status: 'applied',
          pending_restart_keys: [],
          failed_live_keys: {},
        }), { status: 200 })
      }
      if (input === '/api/config/values') {
        return new Response(JSON.stringify({
          revision: 4,
          desired: { ui_settings: { theme: 'dark', fontSize: 16 } },
          active: { ui_settings: { theme: 'dark', fontSize: 16 } },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        }), { status: 200 })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useSettings())
    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => url === '/api/config/values')).toBe(true)
    })

    act(() => result.current.updateTheme('light'))

    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(([, init]) => {
        if (init?.method !== 'PATCH') return false
        return JSON.parse(String(init.body)).values.ui_settings.theme === 'light'
      })
      expect(patchCall).toBeDefined()
      expect(JSON.parse(String(patchCall?.[1]?.body))).toMatchObject({
        expected_revision: 4,
        values: { ui_settings: { theme: 'light', fontSize: 16 } },
      })
    })
  })

  it('normalizes an empty persisted plan pending variant', () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ planPendingVariant: '' }))
    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.planPendingVariant).toBe('info')
  })

  it('applies density to the document, persists it to localStorage, and never sends it to the API', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      return new Response(JSON.stringify(init?.method === 'PATCH' ? {
        committed: true,
        revision: 2,
        changed_keys: [],
        apply_status: 'applied',
        pending_restart_keys: [],
        failed_live_keys: {},
      } : {
        revision: 1,
        desired: { ui_settings: {} },
        active: { ui_settings: {} },
        secret_set: {},
        pending_restart_keys: [],
        failed_live_keys: {},
      }), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useSettings())

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-density', 'comfortable')
    })

    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    fetchMock.mockClear()

    act(() => {
      result.current.updateDensity('compact')
    })

    await waitFor(() => {
      expect(document.documentElement).toHaveAttribute('data-density', 'compact')
    })
    await waitFor(() => {
      const stored = JSON.parse(localStorage.getItem('gobby-settings') ?? '{}')
      expect(stored.density).toBe('compact')
    })

    const patchCall = fetchMock.mock.calls.find(
      ([url, init]) => url === '/api/config/values' && init?.method === 'PATCH',
    )
    expect(patchCall).toBeDefined()
    const body = JSON.parse((patchCall?.[1]?.body as string) ?? '{}')
    expect(body.values.ui_settings).not.toHaveProperty('density')
    expect(body.values.ui_settings).toHaveProperty('theme')
  })

  it('normalizes an out-of-range persisted density to comfortable', () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ density: 'spacious' }))
    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.density).toBe('comfortable')
  })

  it.each([
    [12, 12],
    [24, 24],
    [48, 24],
  ])('normalizes persisted font size %s to %s', (persisted, expected) => {
    localStorage.setItem('gobby-settings', JSON.stringify({ fontSize: persisted }))

    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.fontSize).toBe(expected)
  })

  it.each([null, '18'])('falls back for invalid persisted font size %s', (persisted) => {
    localStorage.setItem('gobby-settings', JSON.stringify({ fontSize: persisted }))

    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.fontSize).toBe(16)
  })

  it('preserves the default font size when local storage omits the field', () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ theme: 'light' }))

    const { result } = renderHook(() => useSettings())

    expect(result.current.settings.fontSize).toBe(16)
  })

  it.each(['invalid-root', null, []])('discards malformed persisted root %j', (persisted) => {
    localStorage.setItem('gobby-settings', JSON.stringify(persisted))

    const { result } = renderHook(() => useSettings())

    expect(result.current.settings).toMatchObject({ fontSize: 16, theme: 'dark' })
  })

  it.each([
    [12, 12],
    [24, 24],
    [48, 24],
    [null, 16],
    ['18', 16],
    [Number.NaN, 16],
    [Number.POSITIVE_INFINITY, 16],
    [undefined, 16],
  ])('normalizes API font size %s to %s', async (remoteFontSize, expected) => {
    const remote = { fontSize: remoteFontSize }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => remote })),
    )

    const { result } = renderHook(() => useSettings())

    await waitFor(() => {
      expect(JSON.parse(localStorage.getItem('gobby-settings') ?? '{}').fontSize).toBe(expected)
    })
    expect(result.current.settings.fontSize).toBe(expected)
  })

  it('preserves a valid local font size when the API omits the field', async () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ fontSize: 20 }))
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ theme: 'light' }) })),
    )

    const { result } = renderHook(() => useSettings())

    await waitFor(() => expect(result.current.settings.theme).toBe('light'))
    expect(result.current.settings.fontSize).toBe(20)
  })

  it('discards a malformed API root while preserving valid local settings', async () => {
    localStorage.setItem('gobby-settings', JSON.stringify({ fontSize: 20 }))
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => 'invalid-root' })),
    )

    const { result } = renderHook(() => useSettings())

    await act(async () => Promise.resolve())
    expect(result.current.settings.fontSize).toBe(20)
  })

  it('preserves and persists changes made while remote settings are loading', async () => {
    let resolveRemote!: (response: Response) => void
    const remoteResponse = new Promise<Response>((resolve) => {
      resolveRemote = resolve
    })
    const fetchMock = vi.fn((url: string | URL | Request, init?: RequestInit) => {
      if (url === '/api/config/values' && init?.method === undefined) {
        return remoteResponse
      }
      return Promise.resolve(new Response(JSON.stringify({
        committed: true,
        revision: 2,
        changed_keys: ['ui_settings.theme'],
        apply_status: 'applied',
        pending_restart_keys: [],
        failed_live_keys: {},
      }), { status: 200 }))
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useSettings())

    act(() => {
      // This matches the initial value but is still an explicit user change.
      result.current.updateTheme('dark')
    })

    expect(fetchMock.mock.calls).toHaveLength(1)

    await act(async () => {
      resolveRemote(
        new Response(JSON.stringify({
          revision: 1,
          desired: { ui_settings: { theme: 'light', fontSize: 18 } },
          active: { ui_settings: { theme: 'light', fontSize: 18 } },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      )
      await remoteResponse
    })

    await waitFor(() => {
      expect(result.current.settings).toMatchObject({ theme: 'dark', fontSize: 18 })
    })
    await waitFor(() => {
      const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH')
      expect(patchCall).toBeDefined()
      expect(JSON.parse(patchCall?.[1]?.body as string)).toMatchObject({
        expected_revision: 1,
        values: { ui_settings: { theme: 'dark', fontSize: 18 } },
      })
    })
  })

  it('replaces the icon cache param while preserving other query params and fragments', () => {
    expect(cacheBustedIconHref('/logo.png?theme=dark&v=1#mask')).toBe(
      '/logo.png?theme=dark&v=2#mask',
    )
  })
})
