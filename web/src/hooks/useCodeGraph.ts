import { useCallback } from 'react'

// ── Types ────────────────────────────────────────────────────────

export interface CodeGraphNode {
  id: string
  name: string
  type: string // file, function, class, method, module, constant, etc.
  kind?: string
  file_path?: string
  line_start?: number
  signature?: string
  language?: string
  symbol_count?: number
  blast_distance?: number
}

export interface CodeGraphLink {
  source: string
  target: string
  type: string // CALLS, IMPORTS, DEFINES
  line?: number
  distance?: number
  weight?: number
}

export interface CodeGraphData {
  nodes: CodeGraphNode[]
  links: CodeGraphLink[]
  center?: string // for blast radius
}

export interface CodeGraphSearchResult {
  id: string
  name: string
  type: string
  kind?: string
  file_path?: string
  line_start?: number
  signature?: string
}

// ── Merge utility ────────────────────────────────────────────────

export function mergeCodeGraphData(
  existing: CodeGraphData,
  incoming: CodeGraphData
): CodeGraphData {
  const nodeMap = new Map(existing.nodes.map(n => [n.id, n]))
  for (const n of incoming.nodes) {
    if (!nodeMap.has(n.id)) nodeMap.set(n.id, n)
  }

  const edgeKey = (l: CodeGraphLink) => {
    const src = typeof l.source === 'object' ? (l.source as any).id : l.source
    const tgt = typeof l.target === 'object' ? (l.target as any).id : l.target
    return `${src}|${l.type}|${tgt}`
  }
  const edgeSet = new Set(existing.links.map(edgeKey))
  const merged = [...existing.links]
  for (const l of incoming.links) {
    const key = edgeKey(l)
    if (!edgeSet.has(key)) {
      edgeSet.add(key)
      merged.push(l)
    }
  }

  return { nodes: [...nodeMap.values()], links: merged }
}

// ── Hook ─────────────────────────────────────────────────────────

function getBaseUrl(): string {
  return ''
}

export function useCodeGraph() {
  const fetchFileGraph = useCallback(async (
    projectId: string,
    limit: number = 200
  ): Promise<CodeGraphData | null> => {
    try {
      const params = new URLSearchParams({ project_id: projectId, limit: String(limit) })
      const res = await fetch(`${getBaseUrl()}/api/code-index/graph?${params}`)
      if (!res.ok) return null
      return await res.json()
    } catch (e) {
      console.error('useCodeGraph: fetchFileGraph failed', e)
      return null
    }
  }, [])

  const expandFile = useCallback(async (
    projectId: string,
    filePath: string
  ): Promise<CodeGraphData | null> => {
    try {
      const params = new URLSearchParams({ project_id: projectId })
      const res = await fetch(`${getBaseUrl()}/api/code-index/graph/file/${encodeURIComponent(filePath)}?${params}`)
      if (!res.ok) return null
      return await res.json()
    } catch (e) {
      console.error('useCodeGraph: expandFile failed', e)
      return null
    }
  }, [])

  const expandSymbol = useCallback(async (
    projectId: string,
    symbolId: string
  ): Promise<CodeGraphData | null> => {
    try {
      const params = new URLSearchParams({ project_id: projectId })
      const res = await fetch(`${getBaseUrl()}/api/code-index/graph/symbol/${encodeURIComponent(symbolId)}/neighbors?${params}`)
      if (!res.ok) return null
      return await res.json()
    } catch (e) {
      console.error('useCodeGraph: expandSymbol failed', e)
      return null
    }
  }, [])

  const fetchBlastRadius = useCallback(async (
    projectId: string,
    opts: { symbolId?: string; filePath?: string; depth?: number }
  ): Promise<CodeGraphData | null> => {
    try {
      const params = new URLSearchParams({ project_id: projectId })
      if (opts.symbolId) params.set('symbol_id', opts.symbolId)
      if (opts.filePath) params.set('file_path', opts.filePath)
      if (opts.depth !== undefined && opts.depth !== null) params.set('depth', String(opts.depth))
      const res = await fetch(`${getBaseUrl()}/api/code-index/graph/blast-radius?${params}`)
      if (!res.ok) return null
      return await res.json()
    } catch (e) {
      console.error('useCodeGraph: fetchBlastRadius failed', e)
      return null
    }
  }, [])

  const searchSymbols = useCallback(async (
    projectId: string,
    query: string
  ): Promise<CodeGraphSearchResult[]> => {
    try {
      const params = new URLSearchParams({ project_id: projectId, q: query })
      const res = await fetch(`${getBaseUrl()}/api/code-index/graph/search?${params}`)
      if (!res.ok) return []
      const data = await res.json()
      return data.results || []
    } catch (e) {
      console.error('useCodeGraph: searchSymbols failed', e)
      return []
    }
  }, [])

  return {
    fetchFileGraph,
    expandFile,
    expandSymbol,
    fetchBlastRadius,
    searchSymbols,
  }
}
