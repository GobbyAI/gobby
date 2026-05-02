import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

let mockFetch: MockFetchInstance

async function loadUseStagesRegistry() {
  const modulePath = '../useStagesRegistry'
  return import(/* @vite-ignore */ modulePath)
}

describe('useStagesRegistry', () => {
  beforeEach(() => {
    mockFetch = createMockFetch()
    mockFetch.mockJsonResponse('/api/stages/registry', {
      registry: [
        {
          name: 'build',
          display_name: 'Build',
          category: 'delivery',
          review_policy: 'required',
          sequence_order: 10,
        },
      ],
    })
  })

  afterEach(() => {
    mockFetch.restore()
  })

  it('test_caches_response', async () => {
    const { useStagesRegistry } = await loadUseStagesRegistry()

    const first = renderHook(() => useStagesRegistry())

    await waitFor(() => expect(first.result.current.isLoading).toBe(false))
    expect(first.result.current.registry).toHaveLength(1)

    const second = renderHook(() => useStagesRegistry())

    await waitFor(() => expect(second.result.current.isLoading).toBe(false))
    expect(second.result.current.registry).toHaveLength(1)
    expect(mockFetch.fn).toHaveBeenCalledTimes(1)
  })
})
