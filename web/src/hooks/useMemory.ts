import { useState, useEffect, useCallback, useRef } from 'react'
import { DEFAULT_GRAPH_LIMITS } from '../components/activity/memory/KnowledgeGraphModel'

function normalizeTags(tags: unknown): string[] | null {
  if (Array.isArray(tags)) return tags
  if (typeof tags === 'string') return tags.split(',').map(t => t.trim()).filter(Boolean)
  return null
}

function normalizeImportance(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0.5
}

function normalizeCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function normalizeString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function normalizeBoolean(value: unknown): boolean {
  return value === true
}

function normalizeNullableString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function normalizeMemory(record: Record<string, unknown>): GobbyMemory {
  return {
    id: normalizeString(record.id),
    memory_type: normalizeString(record.memory_type),
    content: normalizeString(record.content),
    created_at: normalizeString(record.created_at),
    updated_at: normalizeString(record.updated_at),
    project_id: normalizeString(record.project_id),
    is_global: normalizeBoolean(record.is_global),
    source_type: normalizeNullableString(record.source_type),
    source_session_id: normalizeNullableString(record.source_session_id),
    importance: normalizeImportance(record.importance),
    access_count: normalizeCount(record.access_count),
    last_accessed_at: normalizeNullableString(record.last_accessed_at),
    tags: normalizeTags(record.tags),
    deleted_at: normalizeNullableString(record.deleted_at),
    dream_action: normalizeNullableString(record.dream_action),
    last_dreamed_at: normalizeNullableString(record.last_dreamed_at),
  }
}

export interface MemoryCrossRef {
  source_id: string
  target_id: string
  similarity: number
  created_at: string
}

export interface MemoryGraphData {
  memories: GobbyMemory[]
  crossrefs: MemoryCrossRef[]
}

// Which memories a read should surface. Mirrors the backend 3-state contract
// (Dream GC, #17165): 'active' hides dream-flagged rows, 'hidden' shows only
// them, 'all' shows everything.
export type MemoryVisibility = 'active' | 'hidden' | 'all'

export interface GobbyMemory {
  id: string
  memory_type: string
  content: string
  created_at: string
  updated_at: string
  project_id: string
  is_global: boolean
  source_type: string | null
  source_session_id: string | null
  importance: number
  access_count: number
  last_accessed_at: string | null
  tags: string[] | null
  // Dream GC soft-delete fields (#17165). Set once the nightly dream sweep
  // flags a memory; null for active rows and pre-migration snapshots.
  deleted_at: string | null
  dream_action: string | null
  last_dreamed_at: string | null

}

export interface KnowledgeEntity {
  entity_key: string
  name: string
  entity_type: string
  project_id: string | null
  properties: Record<string, unknown>
  /** Active memories mentioning this entity (absent when the daemon fails open). */
  memory_count?: number
  /** Snippet of the most recent active memory mentioning this entity. */
  memory_preview?: string | null
}

export interface KnowledgeRelationship {
  source_key: string
  target_key: string
  type: string
  properties: Record<string, unknown>
}

export interface KnowledgeGraphData {
  entities: KnowledgeEntity[]
  relationships: KnowledgeRelationship[]
}

export interface MemoryFilters {
  projectId: string | null
  memoryType: string | null
  recentOnly: boolean
  search: string
  visibility: MemoryVisibility
}

export interface MemoryStats {
  total_count: number
  by_type: Record<string, number>
  recent_count: number
  avg_importance: number
  project_id: string | null
}

interface CreateMemoryParams {
  content: string
  memory_type?: string
  importance?: number
  project_id?: string
  is_global?: boolean
  tags?: string[]
}

interface UpdateMemoryParams {
  content?: string
  memory_type?: string
  importance?: number
  tags?: string[]
}

const DEBOUNCE_MS = 300

function getBaseUrl(): string {
  return ''
}

export function useMemory(projectId?: string | null) {
  const [memories, setMemories] = useState<GobbyMemory[]>([])
  const [searchResults, setSearchResults] = useState<GobbyMemory[] | null>(null)
  const [stats, setStats] = useState<MemoryStats | null>(null)
  const [filters, setFilters] = useState<MemoryFilters>({
    projectId: projectId ?? null,
    memoryType: null,
    recentOnly: false,
    search: '',
    visibility: 'active',
  })

  // Keep projectId in sync when prop changes
  useEffect(() => {
    setFilters(f => {
      const newId = projectId ?? null
      if (f.projectId === newId) return f
      return { ...f, projectId: newId }
    })
  }, [projectId])
  const [isLoading, setIsLoading] = useState(true)
  const debounceRef = useRef<number | null>(null)

  // Fetch memories list
  const fetchMemories = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({ limit: '100' })
      if (filters.projectId) params.set('project_id', filters.projectId)
      if (filters.memoryType) params.set('memory_type', filters.memoryType)
      params.set('visibility', filters.visibility)

      const response = await fetch(`${baseUrl}/api/memories?${params}`)
      if (response.ok) {
        const data = await response.json()
        const items = (data.memories || []).map((m: Record<string, unknown>) =>
          normalizeMemory(m),
        )
        setMemories(items)
      }
    } catch (e) {
      console.error('Failed to fetch memories:', e)
    } finally {
      setIsLoading(false)
    }
  }, [filters.projectId, filters.memoryType, filters.visibility])

  // Create memory
  const createMemory = useCallback(
    async (params: CreateMemoryParams): Promise<GobbyMemory | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/memories`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const memory = normalizeMemory(await response.json())
          // Refresh list after creation
          await fetchMemories()
          return memory
        }
      } catch (e) {
        console.error('Failed to create memory:', e)
      }
      return null
    },
    [fetchMemories]
  )

  // Update memory
  const updateMemory = useCallback(
    async (memoryId: string, params: UpdateMemoryParams): Promise<GobbyMemory | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/memories/${memoryId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const memory = normalizeMemory(await response.json())
          await fetchMemories()
          return memory
        }
      } catch (e) {
        console.error('Failed to update memory:', e)
      }
      return null
    },
    [fetchMemories]
  )

  // Delete memory
  const deleteMemory = useCallback(
    async (memoryId: string): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/memories/${memoryId}`, {
          method: 'DELETE',
        })
        if (response.ok) {
          await fetchMemories()
          return true
        }
      } catch (e) {
        console.error('Failed to delete memory:', e)
      }
      return false
    },
    [fetchMemories]
  )

  // Restore a dream-flagged (soft-hidden) memory back to active visibility.
  const restoreMemory = useCallback(
    async (memoryId: string): Promise<boolean> => {
      // Optimistic: clear the soft-delete flags locally so the row leaves the
      // hidden view immediately. The refetch below reconciles with the server
      // (and corrects the optimistic state if the request failed).
      setMemories(prev =>
        prev.map(m =>
          m.id === memoryId ? { ...m, deleted_at: null, dream_action: null } : m,
        ),
      )
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/memories/${memoryId}/restore`, {
          method: 'POST',
        })
        if (response.ok) {
          await fetchMemories()
          return true
        }
      } catch (e) {
        console.error('Failed to restore memory:', e)
      }
      await fetchMemories()
      return false
    },
    [fetchMemories]
  )

  const promoteMemoryToGlobal = useCallback(
    async (memoryId: string): Promise<GobbyMemory | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/memories/${memoryId}/promote`, {
          method: 'POST',
        })
        if (response.ok) {
          const memory = normalizeMemory(await response.json())
          await fetchMemories()
          return memory
        }
      } catch (e) {
        console.error('Failed to promote memory:', e)
      }
      return null
    },
    [fetchMemories]
  )

  // Search memories with debounce
  const searchMemories = useCallback(
    (query: string) => {
      if (debounceRef.current) {
        window.clearTimeout(debounceRef.current)
      }

      if (!query.trim()) {
        setSearchResults(null)
        return
      }

      setSearchResults(null)

      debounceRef.current = window.setTimeout(async () => {
        try {
          const baseUrl = getBaseUrl()
          const params = new URLSearchParams({ q: query })
          if (filters.projectId) params.set('project_id', filters.projectId)
          params.set('visibility', filters.visibility)

          const response = await fetch(`${baseUrl}/api/memories/search?${params}`)
          if (response.ok) {
            const data = await response.json()
            setSearchResults(
              (data.results || []).map((m: Record<string, unknown>) =>
                normalizeMemory(m),
              ),
            )
          }
        } catch (e) {
          console.error('Failed to search memories:', e)
        }
      }, DEBOUNCE_MS)
    },
    [filters.projectId, filters.visibility]
  )

  // Fetch stats
  const fetchStats = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams()
      if (filters.projectId) params.set('project_id', filters.projectId)

      const response = await fetch(`${baseUrl}/api/memories/stats?${params}`)
      if (response.ok) {
        setStats(await response.json())
      }
    } catch (e) {
      console.error('Failed to fetch memory stats:', e)
    }
  }, [filters.projectId])

  // Fetch on mount and when filters change
  useEffect(() => {
    fetchMemories()
    fetchStats()
  }, [fetchMemories, fetchStats])

  // Cleanup debounce on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [])

  const refreshMemories = useCallback(() => {
    setIsLoading(true)
    fetchMemories()
    fetchStats()
  }, [fetchMemories, fetchStats])

  const fetchKnowledgeGraph = useCallback(async (
    limit = DEFAULT_GRAPH_LIMITS.entities,
    relationshipLimit = DEFAULT_GRAPH_LIMITS.relationships,
  ): Promise<KnowledgeGraphData | null> => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({
        limit: String(limit),
        relationship_limit: String(relationshipLimit),
      })
      if (filters.projectId) params.set('project_id', filters.projectId)
      params.set('visibility', filters.visibility)
      const response = await fetch(`${baseUrl}/api/memories/graph/entities?${params}`)
      if (response.ok) {
        return await response.json()
      }
    } catch (e) {
      console.error('Failed to fetch knowledge graph:', e)
    }
    return null
  }, [filters.projectId, filters.visibility])

  const fetchEntityNeighbors = useCallback(async (entityKey: string): Promise<KnowledgeGraphData | null> => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams()
      if (filters.projectId) params.set('project_id', filters.projectId)
      params.set('visibility', filters.visibility)
      const response = await fetch(
        `${baseUrl}/api/memories/graph/entities/${encodeURIComponent(entityKey)}/neighbors?${params}`
      )
      if (response.ok) {
        return await response.json()
      }
    } catch (e) {
      console.error('Failed to fetch entity neighbors:', e)
    }
    return null
  }, [filters.projectId, filters.visibility])

  const fetchGraphData = useCallback(async (memoryLimit?: number): Promise<MemoryGraphData | null> => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams()
      if (filters.projectId) params.set('project_id', filters.projectId)
      params.set('visibility', filters.visibility)
      if (memoryLimit !== undefined) params.set('memory_limit', String(memoryLimit))

      const response = await fetch(`${baseUrl}/api/memories/graph?${params}`)
      if (response.ok) {
        const data = await response.json()
        return {
          memories: (data.memories || []).map((m: Record<string, unknown>) =>
            normalizeMemory(m),
          ),
          crossrefs: data.crossrefs || [],
        }
      }
    } catch (e) {
      console.error('Failed to fetch graph data:', e)
    }
    return null
  }, [filters.projectId, filters.visibility])

  return {
    memories,
    searchResults,
    stats,
    isLoading,
    filters,
    setFilters,
    createMemory,
    updateMemory,
    deleteMemory,
    restoreMemory,
    promoteMemoryToGlobal,
    searchMemories,
    refreshMemories,
    fetchGraphData,
    fetchKnowledgeGraph,
    fetchEntityNeighbors,
  }
}

export interface FalkorStatus {
  configured: boolean
  url?: string
}

export function useFalkorStatus() {
  const [falkorStatus, setFalkorStatus] = useState<FalkorStatus | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    async function fetchStatus() {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/admin/status`, { signal: controller.signal })
        if (response.ok) {
          const data = await response.json()
          const falkordb = data.memory?.falkordb
          if (falkordb) {
            setFalkorStatus(falkordb)
          }
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
        console.warn('Failed to fetch falkordb status:', e)
      }
    }
    fetchStatus()
    return () => controller.abort()
  }, [])

  return falkorStatus
}
