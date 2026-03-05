import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { createMockFetch, type MockFetchInstance } from '../../test/mocks/fetch'

// Mock useWebSocketEvent to avoid real WS connections
vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: vi.fn(),
}))

import { useSessions } from '../useSessions'

let mockFetch: MockFetchInstance

const SAMPLE_SESSIONS = [
  {
    id: 'sess-1',
    ref: '#100',
    external_id: 'ext-1',
    source: 'claude',
    project_id: 'proj-1',
    title: 'Test Session',
    status: 'active',
    model: 'claude-4',
    message_count: 5,
    created_at: '2026-03-01T00:00:00Z',
    updated_at: '2026-03-01T12:00:00Z',
    seq_num: 100,
    summary_markdown: null,
    git_branch: 'main',
    usage_input_tokens: 1000,
    usage_output_tokens: 500,
    usage_total_cost_usd: 0.05,
    had_edits: true,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
  },
  {
    id: 'sess-2',
    ref: '#101',
    external_id: 'ext-2',
    source: 'gemini',
    project_id: 'proj-1',
    title: 'Another Session',
    status: 'completed',
    model: null,
    message_count: 10,
    created_at: '2026-03-02T00:00:00Z',
    updated_at: '2026-03-02T12:00:00Z',
    seq_num: 101,
    summary_markdown: null,
    git_branch: null,
    usage_input_tokens: 2000,
    usage_output_tokens: 1000,
    usage_total_cost_usd: 0.1,
    had_edits: false,
    agent_depth: 0,
    chat_mode: null,
    parent_session_id: null,
  },
]

beforeEach(() => {
  mockFetch = createMockFetch()
  mockFetch.mockJsonResponse('/api/sessions', { sessions: SAMPLE_SESSIONS })
  mockFetch.mockJsonResponse('/api/files/projects', [
    { id: 'proj-1', name: 'Test Project', repo_path: '/tmp/test' },
  ])
})

afterEach(() => {
  mockFetch.restore()
  vi.restoreAllMocks()
})

describe('useSessions', () => {
  it('fetches sessions on mount', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.sessions).toHaveLength(2)
    expect(result.current.sessions[0].title).toBe('Test Session')
  })

  it('fetches projects on mount', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.projects).toHaveLength(1))

    expect(result.current.projects[0].name).toBe('Test Project')
  })

  it('filters hidden statuses (deleted, handoff_ready, expired)', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockJsonResponse('/api/sessions', {
      sessions: [
        ...SAMPLE_SESSIONS,
        { ...SAMPLE_SESSIONS[0], id: 'sess-deleted', status: 'deleted' },
        { ...SAMPLE_SESSIONS[0], id: 'sess-expired', status: 'expired' },
        { ...SAMPLE_SESSIONS[0], id: 'sess-handoff', status: 'handoff_ready' },
      ],
    })
    mockFetch.mockJsonResponse('/api/files/projects', [])

    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.sessions).toHaveLength(2)
    expect(result.current.sessions.every(s => !['deleted', 'expired', 'handoff_ready'].includes(s.status))).toBe(true)
  })

  it('sorts sessions by newest first by default', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const filtered = result.current.filteredSessions
    expect(filtered[0].ref).toBe('#101') // newer updated_at
    expect(filtered[1].ref).toBe('#100')
  })

  it('sorts sessions by oldest when sortOrder changed', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, sortOrder: 'oldest' }))
    })

    const filtered = result.current.filteredSessions
    expect(filtered[0].ref).toBe('#100')
    expect(filtered[1].ref).toBe('#101')
  })

  it('filters sessions by search term (title)', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, search: 'another' }))
    })

    expect(result.current.filteredSessions).toHaveLength(1)
    expect(result.current.filteredSessions[0].title).toBe('Another Session')
  })

  it('filters sessions by search term (ref)', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => {
      result.current.setFilters(prev => ({ ...prev, search: '#100' }))
    })

    expect(result.current.filteredSessions).toHaveLength(1)
  })

  it('handles fetch error gracefully', async () => {
    mockFetch.resetRoutes()
    mockFetch.mockErrorResponse('/api/sessions', 500)
    mockFetch.mockJsonResponse('/api/files/projects', [])

    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error).toBeTruthy()
    expect(result.current.sessions).toHaveLength(0)
  })

  it('removeSession removes from list', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.sessions).toHaveLength(2))

    act(() => result.current.removeSession('sess-1'))

    expect(result.current.sessions).toHaveLength(1)
    expect(result.current.sessions[0].id).toBe('sess-2')
  })

  it('markSessionDeleting adds to deletingIds', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.sessions).toHaveLength(2))

    act(() => result.current.markSessionDeleting('sess-1'))

    expect(result.current.deletingIds.has('sess-1')).toBe(true)
  })

  it('confirmSessionDeleted removes by external_id', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.sessions).toHaveLength(2))

    act(() => result.current.confirmSessionDeleted('ext-1'))

    expect(result.current.sessions).toHaveLength(1)
    expect(result.current.sessions[0].external_id).toBe('ext-2')
  })

  it('restoreSession clears deletingIds', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.sessions).toHaveLength(2))

    act(() => result.current.markSessionDeleting('sess-1'))
    expect(result.current.deletingIds.has('sess-1')).toBe(true)

    act(() => result.current.restoreSession('sess-1'))
    expect(result.current.deletingIds.has('sess-1')).toBe(false)
  })

  it('renameSession optimistically updates title', async () => {
    mockFetch.mockJsonResponse('/api/sessions/sess-1/rename', { ok: true })

    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.sessions).toHaveLength(2))

    act(() => {
      result.current.renameSession('sess-1', 'Renamed')
    })

    expect(result.current.sessions.find(s => s.id === 'sess-1')?.title).toBe('Renamed')
  })

  it('refresh re-fetches sessions', async () => {
    const { result } = renderHook(() => useSessions())

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => result.current.refresh())

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))
  })
})
