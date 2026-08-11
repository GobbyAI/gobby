import { afterEach, describe, expect, it, vi } from 'vitest'

import { ConfigurationClient } from '../config'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

interface ServerState {
  revision: number
  desired: Record<string, unknown>
}

/**
 * A minimal reactive-config server double: GET returns the current snapshot,
 * PATCH commits when `expected_revision` matches and bumps the revision.
 */
function makeServer(initial: ServerState) {
  const state = { ...initial, desired: { ...initial.desired } }
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) !== '/api/config/values') {
      throw new Error(`Unexpected request: ${String(input)}`)
    }
    if (init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body)) as {
        expected_revision: number
        values: Record<string, unknown>
      }
      if (body.expected_revision !== state.revision) {
        return jsonResponse(
          {
            error: {
              code: 'revision_conflict',
              message: 'Configuration revision is stale',
              retryable: true,
            },
          },
          409,
        )
      }
      state.revision += 1
      Object.assign(state.desired, body.values)
      return jsonResponse({
        committed: true,
        revision: state.revision,
        changed_keys: Object.keys(body.values),
        apply_status: 'applied',
        pending_restart_keys: [],
        failed_live_keys: {},
      })
    }
    return jsonResponse({
      revision: state.revision,
      desired: { ...state.desired },
      active: { ...state.desired },
      secret_set: {},
      pending_restart_keys: [],
      failed_live_keys: {},
    })
  })
  return { state, fetcher }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ConfigurationClient mutation convergence', () => {
  it('reflects_submitted_values_in_the_snapshot_after_patch', async () => {
    const { fetcher } = makeServer({ revision: 4, desired: { memory: { enabled: true } } })
    const client = new ConfigurationClient(fetcher)
    await client.fetchValues()

    const result = await client.patch({ memory: { enabled: false } })

    expect(result.kind).toBe('success')
    // No torn snapshot: the resolved patch has already refetched, so revision
    // and values advance together.
    expect(client.currentSnapshot?.revision).toBe(5)
    expect(client.currentSnapshot?.desired).toEqual({ memory: { enabled: false } })
  })

  it('never_publishes_a_new_revision_with_stale_values', async () => {
    const { fetcher } = makeServer({ revision: 4, desired: { memory: { enabled: true } } })
    const client = new ConfigurationClient(fetcher)
    await client.fetchValues()

    const published: Array<{ revision: number; desired: unknown }> = []
    client.subscribe((snapshot) => {
      published.push({ revision: snapshot.revision, desired: snapshot.desired })
    })
    await client.patch({ memory: { enabled: false } })

    const atFive = published.filter((snapshot) => snapshot.revision === 5)
    expect(atFive.length).toBeGreaterThan(0)
    for (const snapshot of atFive) {
      expect(snapshot.desired).toEqual({ memory: { enabled: false } })
    }
  })

  it('own_commit_ws_event_leaves_the_converged_snapshot_intact', async () => {
    const { fetcher } = makeServer({ revision: 4, desired: { memory: { enabled: true } } })
    const client = new ConfigurationClient(fetcher)
    await client.fetchValues()
    const result = await client.patch({ memory: { enabled: false } })
    expect(result.kind).toBe('success')

    // The daemon broadcasts the committed revision to every client, including
    // the author. Observing it must not disturb (or be needed for) the
    // already-converged snapshot.
    client.observeRevision(5)

    await vi.waitFor(() => {
      expect(client.currentSnapshot?.revision).toBe(5)
      expect(client.currentSnapshot?.desired).toEqual({ memory: { enabled: false } })
    })
  })

  it('observe_revision_ignores_malformed_ws_payloads', () => {
    const { fetcher } = makeServer({ revision: 1, desired: {} })
    const client = new ConfigurationClient(fetcher)

    for (const value of [undefined, null, 'not-a-number', -1, 1.5, Number.NaN]) {
      expect(() => client.observeRevision(value)).not.toThrow()
    }
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('patch_last_write_wins_retries_once_after_a_conflict', async () => {
    const { state, fetcher } = makeServer({
      revision: 4,
      desired: { ui_settings: { theme: 'dark' } },
    })
    const client = new ConfigurationClient(fetcher)
    await client.fetchValues()
    // Another writer commits first: the server is at 5 while the client
    // believes 4, so the first PATCH conflicts.
    state.revision = 5

    const result = await client.patchLastWriteWins({ ui_settings: { theme: 'light' } })

    expect(result.kind).toBe('success')
    expect(state.desired).toEqual({ ui_settings: { theme: 'light' } })
    const patches = fetcher.mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(patches).toHaveLength(2)
  })

  it('patch_last_write_wins_does_not_loop_on_repeated_conflicts', async () => {
    const { state, fetcher } = makeServer({ revision: 4, desired: {} })
    const client = new ConfigurationClient(fetcher)
    await client.fetchValues()
    // The server stays permanently ahead of whatever the client refreshes to.
    const originalFetcher = fetcher.getMockImplementation()
    fetcher.mockImplementation(async (input, init) => {
      if (init?.method === 'PATCH') state.revision += 1
      return originalFetcher!(input, init)
    })

    const result = await client.patchLastWriteWins({ ui_settings: { theme: 'light' } })

    expect(result.kind).toBe('conflict')
    const patches = fetcher.mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(patches).toHaveLength(2)
  })
})
