import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'

import { useFileChanges } from '../../../hooks/useFileChanges'
import type { ChatMessage } from '../../../types/chat'

describe('useFileChanges — session scoping', () => {
  beforeEach(() => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      let files: { path: string; status: string }[] = []
      if (url.includes('/sessions/sess-A/changes')) {
        files = [{ path: 'src/a.ts', status: 'E' }]
      } else if (url.includes('/sessions/sess-B/changes')) {
        files = [{ path: 'src/b.ts', status: 'W' }]
      }
      return Promise.resolve({
        ok: true,
        json: async () => ({ files }),
      } as Response)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('fetches the viewed session and re-fetches when the session switches', async () => {
    const { result, rerender } = renderHook(
      ({ sid }: { sid: string | null }) => useFileChanges(sid, [], false),
      { initialProps: { sid: 'sess-A' as string | null } },
    )

    await waitFor(() => {
      expect(result.current.changedFiles.map((f) => f.path)).toEqual(['src/a.ts'])
    })

    // Switching the viewed session swaps the Changes contents.
    rerender({ sid: 'sess-B' })
    await waitFor(() => {
      expect(result.current.changedFiles.map((f) => f.path)).toEqual(['src/b.ts'])
    })

    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/sessions/sess-A/changes'))
    expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/sessions/sess-B/changes'))
  })

  it('surfaces an error when the session changes request fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ ok: false, status: 500 } as Response)),
    )

    const { result } = renderHook(() => useFileChanges('sess-A', [], false))

    await waitFor(() => {
      expect(result.current.error).toBe('Could not load changes for this session.')
    })
    expect(result.current.changedFiles).toEqual([])
  })

  it('merges the live message overlay only for the active chat', async () => {
    const messages: ChatMessage[] = [
      {
        id: 'msg-1',
        role: 'assistant',
        content: '',
        timestamp: new Date(0),
        toolCalls: [
          {
            id: 'tool-1',
            status: 'completed',
            tool_name: 'Write',
            server_name: 'builtin',
            tool_type: 'edit',
            arguments: { file_path: 'src/live.ts' },
          },
        ],
      },
    ]

    const { result } = renderHook(() => useFileChanges('sess-A', messages, true))

    await waitFor(() => {
      const paths = result.current.changedFiles.map((f) => f.path)
      expect(paths).toContain('src/a.ts') // from the backend
      expect(paths).toContain('src/live.ts') // from the live overlay
    })
  })
})
