import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

let mockFetch: MockFetchInstance

async function loadUseStagesRegistry() {
  const modulePath = '../useStagesRegistry'
  return import(/* @vite-ignore */ modulePath)
}

describe('useStagesRegistry', () => {
  beforeEach(() => {
    vi.resetModules()
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

  it('normalizes real backend stages response', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse('/api/stages/registry', {
      stages: [
        {
          name: 'operator_review',
          display_label: 'Operator Review',
          category: 'verification',
          review_policy: 'required',
          position_hint: 70,
          updated_at: '2026-05-02T00:00:00Z',
        },
      ],
    })

    const { useStagesRegistry } = await loadUseStagesRegistry()

    const { result } = renderHook(() => useStagesRegistry())

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.registry[0]).toMatchObject({
      name: 'operator_review',
      display_name: 'Operator Review',
      category: 'verification',
      state: 'ready',
      review_policy: 'required',
      position: 70,
      sequence_order: 70,
      updated_at: '2026-05-02T00:00:00Z',
    })
  })
})
