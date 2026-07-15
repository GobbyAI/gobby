import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useFiles } from '../useFiles'

describe('useFiles truncated files', () => {
  beforeEach(() => {
    const fetchMock = vi.fn((input: RequestInfo | URL): Promise<Response> => {
      const url = String(input)
      if (url.includes('/api/files/projects')) {
        return Promise.resolve({ ok: true, json: async () => [] } as Response)
      }
      if (url.includes('/api/files/read')) {
        return Promise.resolve({
          ok: true,
          json: async () => ({
            content: 'first megabyte',
            image: false,
            binary: false,
            truncated: true,
            mime_type: 'text/plain',
            size: 1_048_577,
          }),
        } as Response)
      }
      return Promise.resolve({ ok: true, json: async () => ({}) } as Response)
    })
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('stores the truncated flag and refuses edit or save operations', async () => {
    const { result } = renderHook(() => useFiles())

    await act(async () => {
      await result.current.openFile('project-1', 'large.log', 'large.log')
    })
    await waitFor(() => expect(result.current.openFiles[0]?.truncated).toBe(true))

    act(() => result.current.toggleEditing(0))
    expect(result.current.openFiles[0]?.editing).toBe(false)

    act(() => result.current.updateEditContent(0, 'changed content'))
    await waitFor(() => expect(result.current.openFiles[0]?.dirty).toBe(true))
    await act(async () => {
      await result.current.saveFile(0)
    })

    expect(fetch).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/files/write'),
      expect.anything(),
    )
  })
})
