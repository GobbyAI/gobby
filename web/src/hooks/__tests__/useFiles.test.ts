import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useFiles } from '../useFiles'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('useFiles save failures', () => {
  it('preserves unsaved edits and clears the error after a successful retry', async () => {
    let writeAttempts = 0
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/files/projects')) {
        return { ok: true, json: async () => [] } as Response
      }
      if (url.includes('/api/files/read?')) {
        return {
          ok: true,
          json: async () => ({
            content: 'original',
            image: false,
            binary: false,
            mime_type: 'text/plain',
            size: 8,
          }),
        } as Response
      }
      if (url.endsWith('/api/files/write')) {
        writeAttempts += 1
        return writeAttempts === 1
          ? {
              ok: false,
              status: 503,
              json: async () => ({ detail: 'temporarily unavailable' }),
            } as Response
          : { ok: true, json: async () => ({}) } as Response
      }
      throw new Error(`Unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(() => useFiles())

    await act(async () => {
      await result.current.openFile('project-1', 'notes.txt', 'notes.txt')
    })
    await waitFor(() => expect(result.current.openFiles[0]?.loading).toBe(false))

    act(() => {
      result.current.toggleEditing(0)
      result.current.updateEditContent(0, 'unsaved draft')
    })

    await act(async () => {
      await result.current.saveFile(0)
    })

    expect(result.current.openFiles[0]).toMatchObject({
      editing: true,
      dirty: true,
      editContent: 'unsaved draft',
      saveError: 'Error: temporarily unavailable',
    })

    act(() => result.current.clearSaveError(0))
    expect(result.current.openFiles[0].saveError).toBeNull()

    await act(async () => {
      await result.current.saveFile(0)
    })

    expect(result.current.openFiles[0]).toMatchObject({
      content: 'unsaved draft',
      originalContent: 'unsaved draft',
      editContent: 'unsaved draft',
      dirty: false,
      saveError: null,
    })
    expect(writeAttempts).toBe(2)
  })
})
