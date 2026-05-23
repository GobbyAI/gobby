import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../../../hooks/useMemory', () => ({
  useMemory: () => ({
    memories: [],
    stats: { total_count: 0, by_type: {}, recent_count: 0, avg_importance: 0, project_id: null },
    isLoading: false,
    filters: { projectId: null, memoryType: null, recentOnly: false, search: '' },
    setFilters: vi.fn(),
    createMemory: vi.fn(),
    updateMemory: vi.fn(),
    deleteMemory: vi.fn(),
    refreshMemories: vi.fn(),
    fetchKnowledgeGraph: vi.fn(),
    fetchEntityNeighbors: vi.fn(),
  }),
  useFalkorStatus: vi.fn(() => ({ configured: true, url: 'redis://localhost:6379' })),
}))

vi.mock('../MemoryFilters', () => ({
  MemoryFilters: () => <div data-testid="memory-filters" />,
}))

vi.mock('../MemoryTable', () => ({
  MemoryTable: () => <div data-testid="memory-table" />,
}))

vi.mock('../MemoryForm', () => ({
  MemoryForm: () => <div data-testid="memory-form" />,
}))

vi.mock('../MemoryDetail', () => ({
  MemoryDetail: () => <div data-testid="memory-detail" />,
}))

vi.mock('../../../utils/platform', () => ({
  IS_MOBILE: false,
  IS_IOS: false,
  WEBGL_CAP: { supported: true },
}))

import { useFalkorStatus } from '../../../hooks/useMemory'
import { MemoryPage } from '../MemoryPage'

describe('MemoryPage FalkorDB status', () => {
  it('uses the renamed FalkorDB status hook to enable knowledge graph mode', async () => {
    render(<MemoryPage />)

    await waitFor(() => expect(useFalkorStatus).toHaveBeenCalled())

    expect(screen.getByRole('button', { name: 'Knowledge graph' })).toBeInTheDocument()
  })
})
