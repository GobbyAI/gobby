import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { useSourceControl } from '../useSourceControl'

const PROJECT_PAYLOADS: Record<string, unknown> = {
  status: {
    github_available: true,
    github_repo: 'owner/repo',
    current_branch: 'main',
    branch_count: 1,
    worktree_count: 1,
    clone_count: 1,
  },
  branches: { branches: [{ name: 'main' }] },
  worktrees: { worktrees: [{ id: 'new-worktree' }] },
  clones: { clones: [{ id: 'new-clone' }] },
  prs: { prs: [{ number: 2 }] },
  issues: { issues: [{ number: 3 }] },
  runs: { runs: [{ id: 4 }] },
}

function payloadFor(url: string): unknown {
  if (url.includes('/status?')) return PROJECT_PAYLOADS.status
  if (url.includes('/branches?')) return PROJECT_PAYLOADS.branches
  if (url.includes('/worktrees?')) return PROJECT_PAYLOADS.worktrees
  if (url.includes('/clones?')) return PROJECT_PAYLOADS.clones
  if (url.includes('/prs?')) return PROJECT_PAYLOADS.prs
  if (url.includes('/issues?')) return PROJECT_PAYLOADS.issues
  return PROJECT_PAYLOADS.runs
}

function signalsFor(fetchMock: ReturnType<typeof vi.fn>, projectId: string): AbortSignal[] {
  return fetchMock.mock.calls
    .filter(([input]) => String(input).includes(`project_id=${projectId}`))
    .map(([, init]) => (init as RequestInit).signal as AbortSignal)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, resolve, reject }
}

describe('useSourceControl', () => {
  let originalFetch: typeof globalThis.fetch

  beforeEach(() => {
    originalFetch = globalThis.fetch
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('aborts response bodies from the previous project without committing their state', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = String(input)
      const signal = init?.signal as AbortSignal

      if (url.includes('project_id=old-project')) {
        return Promise.resolve({
          ok: true,
          status: 200,
          statusText: 'OK',
          json: () =>
            new Promise((_, reject) => {
              signal.addEventListener(
                'abort',
                () => reject(new DOMException('The operation was aborted', 'AbortError')),
                { once: true },
              )
            }),
        } as Response)
      }

      return Promise.resolve(
        new Response(JSON.stringify(payloadFor(url)), {
          headers: { 'Content-Type': 'application/json' },
        }),
      )
    })
    globalThis.fetch = fetchMock as typeof fetch

    const { result, rerender, unmount } = renderHook(
      ({ projectId }: { projectId: string }) => useSourceControl(projectId),
      { initialProps: { projectId: 'old-project' } },
    )

    await waitFor(() => expect(signalsFor(fetchMock, 'old-project')).toHaveLength(7))
    const oldSignals = signalsFor(fetchMock, 'old-project')
    expect(oldSignals.every(signal => signal instanceof AbortSignal && !signal.aborted)).toBe(true)

    rerender({ projectId: 'new-project' })

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(oldSignals.every(signal => signal.aborted)).toBe(true)
    expect(result.current.worktrees).toEqual([{ id: 'new-worktree' }])
    expect(result.current.clones).toEqual([{ id: 'new-clone' }])
    expect(result.current.issues).toEqual([{ number: 3 }])
    expect(result.current.error).toBeNull()
    expect(consoleError).not.toHaveBeenCalled()

    const newSignals = signalsFor(fetchMock, 'new-project')
    expect(newSignals).toHaveLength(7)
    unmount()
    expect(newSignals.every(signal => signal.aborted)).toBe(true)
  })

  it('ignores a stale project response when the fetch implementation does not honor abort', async () => {
    const oldBranches = deferred<Response>()
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input)
      if (url.includes('/branches?') && url.includes('project_id=old-project')) {
        return oldBranches.promise
      }
      const payload = url.includes('/branches?')
        ? { branches: [{ name: 'new-project-branch' }] }
        : payloadFor(url)
      return Promise.resolve(Response.json(payload))
    })
    globalThis.fetch = fetchMock as typeof fetch

    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string }) => useSourceControl(projectId),
      { initialProps: { projectId: 'old-project' } },
    )

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/branches?project_id=old-project'),
        expect.any(Object),
      ),
    )
    rerender({ projectId: 'new-project' })
    await waitFor(() =>
      expect(result.current.branches).toEqual([{ name: 'new-project-branch' }]),
    )

    await act(async () => {
      oldBranches.resolve(Response.json({ branches: [{ name: 'stale-project-branch' }] }))
      await oldBranches.promise
    })

    expect(result.current.branches).toEqual([{ name: 'new-project-branch' }])
    expect(result.current.error).toBeNull()
  })

  it('ignores a stale refresh error and keeps the active refresh result', async () => {
    const staleBranches = deferred<Response>()
    let branchRequestCount = 0
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input)
      if (url.includes('/branches?') && branchRequestCount++ === 0) {
        return staleBranches.promise
      }
      const payload = url.includes('/branches?')
        ? { branches: [{ name: 'active-refresh-branch' }] }
        : payloadFor(url)
      return Promise.resolve(Response.json(payload))
    })
    globalThis.fetch = fetchMock as typeof fetch

    const { result } = renderHook(() => useSourceControl('project'))
    await waitFor(() => expect(branchRequestCount).toBe(1))

    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.branches).toEqual([{ name: 'active-refresh-branch' }])

    await act(async () => {
      staleBranches.reject(new Error('stale refresh failed'))
      await expect(staleBranches.promise).rejects.toThrow('stale refresh failed')
    })

    expect(result.current.branches).toEqual([{ name: 'active-refresh-branch' }])
    expect(result.current.error).toBeNull()
    expect(consoleError).not.toHaveBeenCalled()
  })
})
