import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { configurationClient, parseConfigRevision } from '../../api/config'
import { useConfiguration } from '../useConfiguration'

const websocket = vi.hoisted(() => ({
  connected: true,
  handler: null as ((data: Record<string, unknown>) => void) | null,
}))

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketConnected: () => websocket.connected,
  useWebSocketEvent: (eventType: string, handler: (data: Record<string, unknown>) => void) => {
    if (eventType === 'config_event') websocket.handler = handler
  },
}))

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useConfiguration revision authority', () => {
  beforeEach(() => {
    configurationClient.reset()
    websocket.connected = true
    websocket.handler = null
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('includes_revision_in_every_patch', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        return jsonResponse({
          committed: true,
          revision: 8,
          changed_keys: ['ui_settings.theme'],
          apply_status: 'applied',
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/values') {
        return jsonResponse({
          revision: 7,
          desired: { ui_settings: { theme: 'dark' } },
          active: { ui_settings: { theme: 'dark' } },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') {
        return jsonResponse({ type: 'object', properties: {} })
      }
      if (input === '/api/rules') {
        return jsonResponse({ enforcement_enabled: true })
      }
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    await act(async () => {
      await result.current.saveConfig({ ui_settings: { theme: 'light' } })
    })

    const mutation = fetchMock.mock.calls.find(
      ([url, init]) => url === '/api/config/values' && init?.method === 'PATCH',
    )
    expect(mutation).toBeDefined()
    expect(JSON.parse(String(mutation?.[1]?.body))).toEqual({
      expected_revision: 7,
      values: { ui_settings: { theme: 'light' } },
      unset: [],
    })
  })

  it('preserves_draft_after_conflict', async () => {
    let valuesRead = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        return jsonResponse({
          error: {
            code: 'revision_conflict',
            message: 'Configuration revision is stale',
            retryable: true,
          },
        }, 409)
      }
      if (input === '/api/config/values') {
        valuesRead += 1
        const revision = valuesRead === 1 ? 7 : 8
        return jsonResponse({
          revision,
          desired: { ui_settings: { theme: revision === 7 ? 'dark' : 'light' } },
          active: { ui_settings: { theme: revision === 7 ? 'dark' : 'light' } },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const draft = { ui_settings: { theme: 'system' } }
    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    let saveResult: Awaited<ReturnType<typeof result.current.saveConfig>> | undefined
    await act(async () => {
      saveResult = await result.current.saveConfig(draft)
    })

    expect(saveResult).toMatchObject({ ok: false, conflict: true })
    expect(draft).toEqual({ ui_settings: { theme: 'system' } })
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(1)
    expect(result.current.configValues).toEqual({ ui_settings: { theme: 'light' } })
  })

  it('coalesces_config_revision_events', async () => {
    let valuesRead = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        valuesRead += 1
        const revision = valuesRead === 1 ? 7 : 9
        return jsonResponse({
          revision,
          desired: { revisionValue: revision },
          active: { revisionValue: revision },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    act(() => {
      websocket.handler?.({ type: 'config_event', revision: 8 })
      websocket.handler?.({ type: 'config_event', revision: 9 })
    })

    await waitFor(() => expect(result.current.revision).toBe(9))
    expect(valuesRead).toBe(2)
  })

  it('resyncs_on_first_connect_and_retries_a_failed_initial_fetch', async () => {
    let failInitial = true
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        if (failInitial) return jsonResponse({ error: { code: 'unavailable' } }, 503)
        return jsonResponse({
          revision: 3,
          desired: { recovered: true },
          active: { recovered: true },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    // The daemon is unreachable at boot: the socket is down and the initial
    // values fetch fails.
    websocket.connected = false
    const { result, rerender } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    expect(result.current.revision).toBe(0)

    // The daemon comes back: the first-ever connect must resync, which also
    // retries the failed initial fetch.
    failInitial = false
    websocket.connected = true
    rerender()

    await waitFor(() => expect(result.current.revision).toBe(3))
    expect(result.current.configValues).toEqual({ recovered: true })
  })

  it('rederives_rules_and_approvals_from_ws_driven_snapshots', async () => {
    let valuesRead = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        valuesRead += 1
        const first = valuesRead === 1
        return jsonResponse({
          revision: valuesRead,
          desired: {
            rules: { enforcement_enabled: first },
            tool_approvals: { global_rules: first ? [] : ['mcp__gobby__*'] },
          },
          active: {},
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    expect(result.current.rulesEnforcement).toBe(true)
    expect(result.current.globalApprovalRules).toEqual([])

    // A snapshot arriving through the WS revision path (not fetchConfig) must
    // update the derived flags too.
    act(() => websocket.handler?.({ type: 'config_event', revision: 2 }))

    await waitFor(() => expect(result.current.rulesEnforcement).toBe(false))
    expect(result.current.globalApprovalRules).toEqual(['mcp__gobby__*'])
  })

  it('malformed_ws_revision_payloads_are_ignored', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        return jsonResponse({
          revision: 1,
          desired: {},
          active: {},
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })

    // The dispatcher hands the handler whatever the daemon sent; a bad
    // revision must not throw out of the WS dispatch path.
    expect(() => {
      act(() => {
        websocket.handler?.({ type: 'config_event', revision: 'garbage' })
        websocket.handler?.({ type: 'config_event', revision: -3 })
      })
    }).not.toThrow()
    expect(result.current.revision).toBe(1)
  })

  it('surfaces_thrown_mutation_errors_as_mutation_error', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        throw new Error('network unreachable')
      }
      if (input === '/api/config/values') {
        return jsonResponse({
          revision: 1,
          desired: {},
          active: {},
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    let saveResult: Awaited<ReturnType<typeof result.current.saveConfig>> | undefined
    await act(async () => {
      saveResult = await result.current.saveConfig({ ui_settings: { theme: 'light' } })
    })

    expect(saveResult).toMatchObject({ ok: false, errors: ['network unreachable'] })
    expect(result.current.mutationError).toEqual({
      message: 'network unreachable',
      terminal: false,
    })
  })

  it('refetches_on_reconnect', async () => {
    let valuesRead = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        valuesRead += 1
        return jsonResponse({
          revision: valuesRead,
          desired: { connectedValue: valuesRead },
          active: { connectedValue: valuesRead },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result, rerender } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    websocket.connected = false
    rerender()
    websocket.connected = true
    rerender()

    await waitFor(() => expect(result.current.revision).toBe(2))
    expect(valuesRead).toBe(2)
  })

  it('watermark_triggers_trailing_refetch', async () => {
    let valuesRead = 0
    let resolveStale!: () => void
    const staleGate = new Promise<void>((resolve) => {
      resolveStale = resolve
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      if (input === '/api/config/values') {
        valuesRead += 1
        if (valuesRead === 2) await staleGate
        const revision = valuesRead === 1 ? 7 : valuesRead === 2 ? 8 : 9
        return jsonResponse({
          revision,
          desired: { watermarkValue: revision },
          active: { watermarkValue: revision },
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    act(() => websocket.handler?.({ type: 'config_event', revision: 8 }))
    await waitFor(() => expect(valuesRead).toBe(2))
    act(() => websocket.handler?.({ type: 'config_event', revision: 9 }))
    resolveStale()

    await waitFor(() => expect(result.current.revision).toBe(9))
    expect(result.current.configValues).toEqual({ watermarkValue: 9 })
    expect(valuesRead).toBe(3)
  })

  it('round_trips_codec_vectors', async () => {
    const vectors = {
      dynamic: {
        '~': { '%2E': 'literal-dot' },
        'a.b': { '__gobby_key__': 'sentinel-text' },
        '100%': ['%', '.', '~', '__gobby_key__:'],
      },
    }
    const canonical = JSON.stringify(vectors)
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        expect(JSON.stringify(JSON.parse(String(init.body)).values)).toBe(canonical)
        return jsonResponse({
          committed: true,
          revision: 2,
          changed_keys: ['dynamic'],
          apply_status: 'applied',
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/values') {
        return jsonResponse({
          revision: 1,
          desired: vectors,
          active: vectors,
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    expect(JSON.stringify(result.current.configValues)).toBe(canonical)
    await act(async () => {
      await result.current.saveConfig(result.current.configValues)
    })
    expect(parseConfigRevision(0)).toBe(0)
    expect(parseConfigRevision(Number.MAX_SAFE_INTEGER)).toBe(Number.MAX_SAFE_INTEGER)
    expect(() => parseConfigRevision(Number.MAX_SAFE_INTEGER + 1)).toThrow(TypeError)
    expect(() => parseConfigRevision(1.5)).toThrow(TypeError)
  })

  it('exhausted_revision_is_terminal', async () => {
    let valuesRead = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === '/api/config/values' && init?.method === 'PATCH') {
        return jsonResponse({
          error: {
            code: 'revision_exhausted',
            message: 'Configuration revision cannot be advanced',
            retryable: false,
          },
        }, 400)
      }
      if (input === '/api/config/values') {
        valuesRead += 1
        return jsonResponse({
          revision: Number.MAX_SAFE_INTEGER,
          desired: {},
          active: {},
          secret_set: {},
          pending_restart_keys: [],
          failed_live_keys: {},
        })
      }
      if (input === '/api/config/schema') return jsonResponse({ type: 'object', properties: {} })
      if (input === '/api/config/tool-approvals/global') {
        return jsonResponse({ rules: [], default_rules: [], built_in_exemptions: [] })
      }
      throw new Error(`Unexpected request: ${String(input)}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useConfiguration())
    await act(async () => {
      await result.current.fetchConfig()
    })
    let saveResult: Awaited<ReturnType<typeof result.current.saveConfig>> | undefined
    await act(async () => {
      saveResult = await result.current.saveConfig({ ui_settings: { theme: 'light' } })
    })

    expect(saveResult).toMatchObject({ ok: false, terminal: true })
    expect(result.current.mutationError).toEqual({
      message: 'Configuration revision cannot be advanced',
      terminal: true,
    })
    expect(valuesRead).toBe(1)
  })
})
