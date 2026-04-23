import { useState, useEffect, useCallback, useMemo } from 'react'

export interface AgentDefInfo {
  definition: {
    name: string
    description?: string | null
    role?: string | null
    mode?: string
    provider?: string
    model?: string | null
    isolation?: string | null
    surfaces?: string[] | null
  }
  source: string
  sources?: string[] | null
  db_id: string | null
  has_template_update?: boolean
}

interface UseAgentDefinitionsOptions {
  sourceFilter?: string
  surfaceFilter?: string
}

export function useAgentDefinitions(
  projectId?: string | null,
  options?: UseAgentDefinitionsOptions,
) {
  const [definitions, setDefinitions] = useState<AgentDefInfo[]>([])
  const [loading, setLoading] = useState(false)
  const sourceFilter = options?.sourceFilter
  const surfaceFilter = options?.surfaceFilter

  const fetchDefs = useCallback(async () => {
    setLoading(true)
    try {
      const searchParams = new URLSearchParams()
      if (projectId) searchParams.set('project_id', projectId)
      if (sourceFilter) searchParams.set('source_filter', sourceFilter)
      if (surfaceFilter) searchParams.set('surface_filter', surfaceFilter)
      const qs = searchParams.toString()
      const params = qs ? `?${qs}` : ''
      const res = await fetch(`/api/agents/definitions${params}`)
      if (res.ok) {
        const data = await res.json()
        setDefinitions(data.definitions || [])
      }
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [projectId, sourceFilter, surfaceFilter])

  useEffect(() => {
    fetchDefs()
  }, [fetchDefs])

  const globalDefs = useMemo(() => {
    const seen = new Set<string>()
    return definitions
      .filter((d) => d.source === 'installed')
      .filter((d) => {
        if (seen.has(d.definition.name)) return false
        seen.add(d.definition.name)
        return true
      })
  }, [definitions])

  const projectDefs = useMemo(() => {
    const seen = new Set<string>()
    return definitions
      .filter((d) => d.source === 'project')
      .filter((d) => {
        if (seen.has(d.definition.name)) return false
        seen.add(d.definition.name)
        return true
      })
  }, [definitions])

  const hasGlobal = globalDefs.length > 0
  const hasProject = projectDefs.length > 0
  const showScopeToggle = hasGlobal && hasProject

  return {
    definitions,
    globalDefs,
    projectDefs,
    hasGlobal,
    hasProject,
    showScopeToggle,
    loading,
    refresh: fetchDefs,
  }
}
