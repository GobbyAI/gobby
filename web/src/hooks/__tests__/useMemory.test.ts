import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

import { useMemory, useNeo4jStatus } from '../useMemory'

let mockFetch: MockFetchInstance

const SAMPLE_MEMORIES = [
  {
    id: 'mem-1',
    memory_type: 'fact',
    content: 'TypeScript uses structural typing',
    created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T00:00:00Z',
    project_id: 'proj-1',
    source_type: 'session',
    source_session_id: 'sess-1',
    importance: 0.8,
    access_count: 3,
    last_accessed_at: '2026-03-01T12:00:00Z',
    tags: ['typescript', 'types'],
  },
  {
    id: 'mem-2',
    memory_type: 'convention',
    content: 'Use vi.fn() for mocks',
    created_at: '2026-03-02T00:00:00Z',
    updated_at: '2026-03-02T00:00:00Z',
    project_id: null,
    source_type: null,
    source_session_id: null,
    importance: 'invalid', // test normalizeImportance
    access_count: 0,
    last_accessed_at: null,
    tags: 'testing,vitest', // test normalizeTags string form
  },
]

beforeEach(() => {
  mockFetch = createMockFetch()
  mockFetch.mockJsonResponse('/api/memories?', { memories: SAMPLE_MEMORIES })
  mockFetch.mockJsonResponse('/api/memories/stats', {
    total_count: 2,
    by_type: { fact: 1, convention: 1 },
    recent_count: 1,
    avg_importance: 0.65,
    project_id: null,
  })
})

afterEach(() => {
  mockFetch.restore()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useMemory', () => {
  it('fetches memories on mount', async () => {
    const { result } = renderHook(() => useMemory())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.memories).toHaveLength(2)
  })

  it('normalizes importance to number', async () => {
    const { result } = renderHook(() => useMemory())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.memories[0].importance).toBe(0.8)
    expect(result.current.memories[1].importance).toBe(0.5) // fallback for non-number
  })

  it('normalizes tags from comma-separated string', async () => {
    const { result } = renderHook(() => useMemory())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.memories[0].tags).toEqual(['typescript', 'types'])
    expect(result.current.memories[1].tags).toEqual(['testing', 'vitest'])
  })

  it('fetches stats on mount', async () => {
    const { result } = renderHook(() => useMemory())

    await waitFor(() => expect(result.current.stats).toBeTruthy())

    expect(result.current.stats?.total_count).toBe(2)
    expect(result.current.stats?.by_type).toEqual({ fact: 1, convention: 1 })
  })

  it('createMemory posts and re-fetches', async () => {
    const newMem = { ...SAMPLE_MEMORIES[0], id: 'mem-3', content: 'New memory' }
    mockFetch.mockJsonResponse('/api/memories', newMem)

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const created = await act(() =>
      result.current.createMemory({ content: 'New memory', memory_type: 'fact' }),
    )

    expect(created).toBeTruthy()
    expect(created?.content).toBe('New memory')
  })

  it('updateMemory puts and re-fetches', async () => {
    const updated = { ...SAMPLE_MEMORIES[0], content: 'Updated' }
    mockFetch.mockJsonResponse('/api/memories/mem-1', updated)

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const mem = await act(() =>
      result.current.updateMemory('mem-1', { content: 'Updated' }),
    )

    expect(mem?.content).toBe('Updated')
  })

  it('deleteMemory deletes and re-fetches', async () => {
    mockFetch.mockJsonResponse('/api/memories/mem-1', { ok: true })

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const ok = await act(() => result.current.deleteMemory('mem-1'))

    expect(ok).toBe(true)
  })

  it('searchMemories debounces and fetches results', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch.mockJsonResponse('/api/memories/search', {
      results: [SAMPLE_MEMORIES[0]],
    })

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => result.current.searchMemories('typescript'))

    // Should not have results yet (debounced)
    expect(result.current.searchResults).toBeNull()

    // Advance past debounce (300ms)
    await act(async () => {
      vi.advanceTimersByTime(400)
    })

    await waitFor(() => expect(result.current.searchResults).toBeTruthy())

    expect(result.current.searchResults).toHaveLength(1)
  })

  it('searchMemories clears results for empty query', async () => {
    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => result.current.searchMemories(''))

    expect(result.current.searchResults).toBeNull()
  })

  it('refreshMemories re-fetches memories and stats', async () => {
    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => result.current.refreshMemories())

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))
  })

  it('fetchGraphData returns memory graph', async () => {
    mockFetch.mockJsonResponse('/api/memories/graph', {
      memories: SAMPLE_MEMORIES,
      crossrefs: [{ source_id: 'mem-1', target_id: 'mem-2', similarity: 0.9, created_at: '2026-03-01' }],
    })

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const graph = await act(() => result.current.fetchGraphData())

    expect(graph?.memories).toHaveLength(2)
    expect(graph?.crossrefs).toHaveLength(1)
  })

  it('fetchKnowledgeGraph returns entities and relationships', async () => {
    mockFetch.mockJsonResponse('/api/memories/graph/entities', {
      entities: [{ name: 'React', type: 'technology', properties: {} }],
      relationships: [],
    })

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const kg = await act(() => result.current.fetchKnowledgeGraph())

    expect(kg?.entities).toHaveLength(1)
    expect(kg?.entities[0].name).toBe('React')
  })

  it('fetchEntityNeighbors returns neighbors', async () => {
    mockFetch.mockJsonResponse('/api/memories/graph/entities/React/neighbors', {
      entities: [{ name: 'Vite', type: 'technology', properties: {} }],
      relationships: [{ source: 'React', target: 'Vite', type: 'used_with', properties: {} }],
    })

    const { result } = renderHook(() => useMemory())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const neighbors = await act(() => result.current.fetchEntityNeighbors('React'))

    expect(neighbors?.entities).toHaveLength(1)
    expect(neighbors?.relationships).toHaveLength(1)
  })

  it('handles fetch error gracefully', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockErrorResponse('/api/memories', 500)
    mockFetch.mockErrorResponse('/api/memories/stats', 500)

    const { result } = renderHook(() => useMemory())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.memories).toHaveLength(0)
  })
})

describe('useNeo4jStatus', () => {
  it('fetches neo4j status from admin endpoint', async () => {
    mockFetch.mockJsonResponse('/api/admin/status', {
      memory: { neo4j: { configured: true, url: 'bolt://localhost:7687' } },
    })

    const { result } = renderHook(() => useNeo4jStatus())

    await waitFor(() => expect(result.current).toBeTruthy())

    expect(result.current?.configured).toBe(true)
    expect(result.current?.url).toBe('bolt://localhost:7687')
  })

  it('returns null when neo4j not configured', async () => {
    mockFetch.mockJsonResponse('/api/admin/status', {
      memory: {},
    })

    const { result } = renderHook(() => useNeo4jStatus())

    // Wait for the fetch to complete
    await waitFor(() => {
      expect(mockFetch.fn).toHaveBeenCalledWith(
        expect.stringContaining('/api/admin/status'),
        expect.anything(),
      )
    })

    // Result should remain null since neo4j is not in response
    expect(result.current).toBeNull()
  })
})
