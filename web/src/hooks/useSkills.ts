import { useState, useEffect, useCallback, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface GobbySkill {
  id: string
  name: string
  description: string
  content: string
  version: string | null
  license: string | null
  compatibility: string | null
  allowed_tools: string[] | null
  metadata: Record<string, unknown> | null
  source_path: string | null
  source_type: string | null
  source_ref: string | null
  source: string | null
  hub_name: string | null
  hub_slug: string | null
  hub_version: string | null
  enabled: boolean
  always_apply: boolean
  injection_format: string
  project_id: string | null
  deleted_at: string | null
  created_at: string
  updated_at: string
}

export interface SkillStats {
  total: number
  enabled: number
  disabled: number
  bundled: number
  from_hubs: number
  templates: number
  installed_count: number
  by_category: Record<string, number>
  by_source_type: Record<string, number>
}

export interface SkillFilters {
  projectId: string | null
  enabled: boolean | null
  category: string | null
  search: string
  includeTemplates: boolean
  includeDeleted: boolean
}

export interface HubInfo {
  name: string
  type: string
  base_url: string | null
  repo: string | null
}

export interface HubSkillResult {
  slug: string
  display_name: string
  description: string
  hub_name: string
  version: string | null
  score: number | null
}

export interface ScanFinding {
  severity: string
  title: string
  description: string
  category: string
  remediation: string
  location: string
}

export interface ScanResult {
  is_safe: boolean
  max_severity: string
  scan_duration_seconds: number
  findings: ScanFinding[]
  findings_count: number
}

interface CreateSkillParams {
  name: string
  description: string
  content: string
  version?: string
  license?: string
  compatibility?: string
  allowed_tools?: string[]
  metadata?: Record<string, unknown>
  enabled?: boolean
  always_apply?: boolean
  injection_format?: string
  project_id?: string | null
}

interface UpdateSkillParams {
  name?: string
  description?: string
  content?: string
  version?: string
  license?: string
  compatibility?: string
  allowed_tools?: string[]
  metadata?: Record<string, unknown>
  enabled?: boolean
  always_apply?: boolean
  injection_format?: string
}

const DEBOUNCE_MS = 300

function getBaseUrl(): string {
  return ''
}

export function useSkills() {
  const [skills, setSkills] = useState<GobbySkill[]>([])
  const [stats, setStats] = useState<SkillStats | null>(null)
  const [filters, setFilters] = useState<SkillFilters>({
    projectId: null,
    enabled: null,
    category: null,
    search: '',
    includeTemplates: false,
    includeDeleted: false,
  })
  const [isLoading, setIsLoading] = useState(true)
  const [hubs, setHubs] = useState<HubInfo[]>([])
  const [hubResults, setHubResults] = useState<HubSkillResult[]>([])
  const debounceRef = useRef<number | null>(null)
  const wsDebounceRef = useRef<number | null>(null)

  // Fetch skills list
  const fetchSkills = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({ limit: '200' })
      if (filters.projectId) params.set('project_id', filters.projectId)
      if (filters.enabled !== null) params.set('enabled', String(filters.enabled))
      if (filters.category) params.set('category', filters.category)
      if (filters.includeTemplates) params.set('include_templates', 'true')
      if (filters.includeDeleted) params.set('include_deleted', 'true')

      const response = await fetch(`${baseUrl}/api/skills?${params}`)
      if (response.ok) {
        const data = await response.json()
        setSkills(data.skills || [])
      }
    } catch (e) {
      console.error('Failed to fetch skills:', e)
    } finally {
      setIsLoading(false)
    }
  }, [filters.projectId, filters.enabled, filters.category, filters.includeTemplates, filters.includeDeleted])

  // Fetch stats
  const fetchStats = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams()
      if (filters.projectId) params.set('project_id', filters.projectId)

      const response = await fetch(`${baseUrl}/api/skills/stats?${params}`)
      if (response.ok) {
        setStats(await response.json())
      }
    } catch (e) {
      console.error('Failed to fetch skill stats:', e)
    }
  }, [filters.projectId])

  // Create skill
  const createSkill = useCallback(
    async (params: CreateSkillParams): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const skill = await response.json()
          await fetchSkills()
          await fetchStats()
          return skill
        }
        const err = await response.json().catch(() => null)
        throw new Error(err?.detail || `HTTP ${response.status}`)
      } catch (e) {
        console.error('Failed to create skill:', e)
        throw e
      }
    },
    [fetchSkills, fetchStats]
  )

  // Update skill
  const updateSkill = useCallback(
    async (skillId: string, params: UpdateSkillParams): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/${skillId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params),
        })
        if (response.ok) {
          const skill = await response.json()
          await fetchSkills()
          await fetchStats()
          return skill
        }
      } catch (e) {
        console.error('Failed to update skill:', e)
      }
      return null
    },
    [fetchSkills, fetchStats]
  )

  // Delete skill
  const deleteSkill = useCallback(
    async (skillId: string): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/${skillId}`, {
          method: 'DELETE',
        })
        if (response.ok) {
          await fetchSkills()
          await fetchStats()
          return true
        }
      } catch (e) {
        console.error('Failed to delete skill:', e)
      }
      return false
    },
    [fetchSkills, fetchStats]
  )

  // Toggle skill enabled/disabled
  const toggleSkill = useCallback(
    async (skillId: string, enabled: boolean): Promise<boolean> => {
      const result = await updateSkill(skillId, { enabled })
      return result !== null
    },
    [updateSkill]
  )

  // Search skills with debounce
  const searchSkills = useCallback(
    (query: string) => {
      if (debounceRef.current) {
        window.clearTimeout(debounceRef.current)
      }

      if (!query.trim()) {
        return
      }

      debounceRef.current = window.setTimeout(async () => {
        try {
          const baseUrl = getBaseUrl()
          const params = new URLSearchParams({ q: query })
          if (filters.projectId) params.set('project_id', filters.projectId)

          const response = await fetch(`${baseUrl}/api/skills/search?${params}`)
          if (response.ok) {
            const data = await response.json()
            setSkills(data.results || [])
          }
        } catch (e) {
          console.error('Failed to search skills:', e)
        }
      }, DEBOUNCE_MS)
    },
    [filters.projectId]
  )

  // Import skill from source
  const importSkill = useCallback(
    async (source: string, projectId?: string | null): Promise<{ imported: number; skills: GobbySkill[] } | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/import`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source, project_id: projectId }),
        })
        if (response.ok) {
          const result = await response.json()
          await fetchSkills()
          await fetchStats()
          return result
        }
        const err = await response.json().catch(() => null)
        throw new Error(err?.detail || `HTTP ${response.status}`)
      } catch (e) {
        console.error('Failed to import skill:', e)
        throw e
      }
    },
    [fetchSkills, fetchStats]
  )

  // Export skill
  const exportSkill = useCallback(async (skillId: string): Promise<{ filename: string; content: string } | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/skills/${skillId}/export`)
      if (response.ok) {
        return await response.json()
      }
    } catch (e) {
      console.error('Failed to export skill:', e)
    }
    return null
  }, [])

  // Restore defaults
  const restoreDefaults = useCallback(async (): Promise<Record<string, unknown> | null> => {
    try {
      const response = await fetch(`${getBaseUrl()}/skills/restore-defaults`, {
        method: 'POST',
      })
      if (response.ok) {
        const result = await response.json()
        await fetchSkills()
        await fetchStats()
        return result
      }
    } catch (e) {
      console.error('Failed to restore defaults:', e)
    }
    return null
  }, [fetchSkills, fetchStats])

  // Scan skill content
  const scanSkill = useCallback(async (content: string, name?: string): Promise<ScanResult | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/skills/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, name: name || 'untitled' }),
      })
      if (response.ok) {
        return await response.json()
      }
      if (response.status === 501) {
        throw new Error('skill-scanner not installed')
      }
    } catch (e) {
      console.error('Failed to scan skill:', e)
      throw e
    }
    return null
  }, [])

  // Fetch hubs
  const fetchHubs = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/skills/hubs`)
      if (response.ok) {
        const data = await response.json()
        setHubs(data.hubs || [])
      }
    } catch (e) {
      console.error('Failed to fetch hubs:', e)
    }
  }, [])

  // Search hub
  const [hubErrors, setHubErrors] = useState<Record<string, string>>({})

  const searchHub = useCallback(async (query: string, hubName?: string) => {
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams({ q: query })
      if (hubName) params.set('hub_name', hubName)

      const response = await fetch(`${baseUrl}/api/skills/hubs/search?${params}`)
      if (response.ok) {
        const data = await response.json()
        setHubResults(data.results || [])
        setHubErrors(data.hub_errors || {})
      }
    } catch (e) {
      console.error('Failed to search hub:', e)
    }
  }, [])

  // Install from hub
  const installFromHub = useCallback(
    async (hubName: string, slug: string, version?: string, projectId?: string | null): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/hubs/install`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hub_name: hubName, slug, version, project_id: projectId }),
        })
        if (response.ok) {
          const data = await response.json()
          await fetchSkills()
          await fetchStats()
          return data.skill
        }
        const err = await response.json().catch(() => null)
        throw new Error(err?.detail || `HTTP ${response.status}`)
      } catch (e) {
        console.error('Failed to install from hub:', e)
        throw e
      }
    },
    [fetchSkills, fetchStats]
  )

  // Install from template
  const installFromTemplate = useCallback(
    async (skillId: string): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/${skillId}/install`, { method: 'POST' })
        if (response.ok) {
          const data = await response.json()
          await fetchSkills()
          await fetchStats()
          return data.skill
        }
        const err = await response.json().catch(() => null)
        throw new Error(err?.detail || `HTTP ${response.status}`)
      } catch (e) {
        console.error('Failed to install from template:', e)
        throw e
      }
    },
    [fetchSkills, fetchStats]
  )

  // Move to project scope
  const moveToProject = useCallback(
    async (skillId: string, projectId: string): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const params = new URLSearchParams({ project_id: projectId })
        const response = await fetch(`${baseUrl}/api/skills/${skillId}/move-to-project?${params}`, { method: 'POST' })
        if (response.ok) {
          const data = await response.json()
          await fetchSkills()
          await fetchStats()
          return data.skill
        }
      } catch (e) {
        console.error('Failed to move skill to project:', e)
      }
      return null
    },
    [fetchSkills, fetchStats]
  )

  // Move to global (installed) scope
  const moveToGlobal = useCallback(
    async (skillId: string): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/${skillId}/move-to-installed`, { method: 'POST' })
        if (response.ok) {
          const data = await response.json()
          await fetchSkills()
          await fetchStats()
          return data.skill
        }
      } catch (e) {
        console.error('Failed to move skill to global:', e)
      }
      return null
    },
    [fetchSkills, fetchStats]
  )

  // Restore a soft-deleted skill
  const restoreSkill = useCallback(
    async (skillId: string): Promise<GobbySkill | null> => {
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/skills/${skillId}/restore`, { method: 'POST' })
        if (response.ok) {
          const data = await response.json()
          await fetchSkills()
          await fetchStats()
          return data.skill
        }
      } catch (e) {
        console.error('Failed to restore skill:', e)
      }
      return null
    },
    [fetchSkills, fetchStats]
  )

  // Fetch on mount and when filters change
  useEffect(() => {
    fetchSkills()
    fetchStats()
  }, [fetchSkills, fetchStats])

  // Cleanup debounce timers on unmount
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
      if (wsDebounceRef.current) window.clearTimeout(wsDebounceRef.current)
    }
  }, [])

  // Real-time updates via WebSocket
  useWebSocketEvent(
    'skill_event',
    useCallback(() => {
      if (wsDebounceRef.current) window.clearTimeout(wsDebounceRef.current)
      wsDebounceRef.current = window.setTimeout(() => {
        fetchSkills()
        fetchStats()
      }, 500)
    }, [fetchSkills, fetchStats]),
  )

  const refreshSkills = useCallback(() => {
    setIsLoading(true)
    fetchSkills()
    fetchStats()
  }, [fetchSkills, fetchStats])

  return {
    skills,
    stats,
    isLoading,
    filters,
    setFilters,
    createSkill,
    updateSkill,
    deleteSkill,
    toggleSkill,
    searchSkills,
    importSkill,
    exportSkill,
    restoreDefaults,
    scanSkill,
    refreshSkills,
    hubs,
    hubResults,
    hubErrors,
    fetchHubs,
    searchHub,
    installFromHub,
    installFromTemplate,
    moveToProject,
    moveToGlobal,
    restoreSkill,
  }
}
