import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface RuleSummary {
  id: string
  name: string
  description: string | null
  event: string | null
  group: string | null
  when: string | null
  enabled: boolean
  priority: number
  source: string
  tags: string[] | null
  effect: Record<string, unknown> | null
}

export interface RuleDetail extends RuleSummary {
  match: Record<string, unknown> | null
  effect: Record<string, unknown> | null
}

function getBaseUrl(): string {
  return ''
}

export function useRules() {
  const [rules, setRules] = useState<RuleSummary[]>([])
  const [groups, setGroups] = useState<string[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [enforcementEnabled, setEnforcementEnabled] = useState(true)

  const fetchRules = useCallback(async (params?: {
    event?: string
    group?: string
    enabled?: boolean
  }) => {
    try {
      const baseUrl = getBaseUrl()
      const searchParams = new URLSearchParams()
      if (params?.event) searchParams.set('event', params.event)
      if (params?.group) searchParams.set('group', params.group)
      if (params?.enabled !== undefined) searchParams.set('enabled', String(params.enabled))
      const query = searchParams.toString()
      const url = `${baseUrl}/api/rules${query ? `?${query}` : ''}`

      const response = await fetch(url)
      if (response.ok) {
        const data = await response.json()
        setRules(data.rules || [])
        if (data.enforcement_enabled !== undefined) {
          setEnforcementEnabled(data.enforcement_enabled)
        }
      }
    } catch (e) {
      console.error('Failed to fetch rules:', e)
    }
  }, [])

  const fetchGroups = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules/groups`)
      if (response.ok) {
        const data = await response.json()
        setGroups(data.groups || [])
      }
    } catch (e) {
      console.error('Failed to fetch rule groups:', e)
    }
  }, [])

  const fetchRuleDetail = useCallback(async (name: string): Promise<RuleDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules/${encodeURIComponent(name)}`)
      if (response.ok) {
        const data = await response.json()
        return data.rule || null
      }
    } catch (e) {
      console.error('Failed to fetch rule detail:', e)
    }
    return null
  }, [])

  const toggleRule = useCallback(async (name: string, enabled: boolean): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules/${encodeURIComponent(name)}/toggle`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchRules()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to toggle rule:', e)
    }
    return false
  }, [fetchRules])

  const createRule = useCallback(async (name: string, definition: Record<string, unknown>): Promise<RuleDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, definition }),
      })
      if (response.ok) {
        const data = await response.json()
        await fetchRules()
        return data.rule || null
      }
    } catch (e) {
      console.error('Failed to create rule:', e)
    }
    return null
  }, [fetchRules])

  const updateRule = useCallback(async (name: string, definition: Record<string, unknown>): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules/${encodeURIComponent(name)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ definition }),
      })
      if (response.ok) {
        await fetchRules()
        return true
      }
    } catch (e) {
      console.error('Failed to update rule:', e)
    }
    return false
  }, [fetchRules])

  const installFromTemplate = useCallback(async (id: string): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/install`, {
        method: 'POST',
      })
      const data = await response.json().catch(() => ({}))
      if (response.ok && data.status === 'success') {
        await fetchRules()
        return true
      }
      window.alert(data.detail || 'Failed to install rule from template')
    } catch (e) {
      console.error('Failed to install rule from template:', e)
      window.alert(`Failed to install from template: ${e instanceof Error ? e.message : String(e)}`)
    }
    return false
  }, [fetchRules])

  const bulkToggleRules = useCallback(async (source: string, enabled: boolean): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules/bulk-toggle`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source, enabled }),
      })
      if (response.ok) {
        await fetchRules()
        return true
      }
    } catch (e) {
      console.error('Failed to bulk toggle rules:', e)
    }
    return false
  }, [fetchRules])

  const setEnforcement = useCallback(async (enabled: boolean): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/rules`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enforcement_enabled: enabled }),
      })
      if (response.ok) {
        const data = await response.json()
        setEnforcementEnabled(data.enforcement_enabled)
        return true
      }
    } catch (e) {
      console.error('Failed to set enforcement:', e)
    }
    return false
  }, [])

  const deleteRule = useCallback(async (name: string, force?: boolean): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const params = force ? '?force=true' : ''
      const response = await fetch(`${baseUrl}/api/rules/${encodeURIComponent(name)}${params}`, {
        method: 'DELETE',
      })
      if (response.ok) {
        await fetchRules()
        return true
      }
    } catch (e) {
      console.error('Failed to delete rule:', e)
    }
    return false
  }, [fetchRules])

  // Computed values
  const ruleCount = rules.length
  const enabledCount = useMemo(() => rules.filter(r => r.enabled).length, [rules])

  const eventTypes = useMemo(() => {
    const set = new Set<string>()
    rules.forEach(r => { if (r.event) set.add(r.event) })
    return Array.from(set).sort()
  }, [rules])

  const sources = useMemo(() => {
    const set = new Set<string>()
    rules.forEach(r => { if (r.source) set.add(r.source) })
    return Array.from(set).sort()
  }, [rules])

  // Auto-fetch on mount
  const debounceRef = useRef<number | null>(null)
  
  useEffect(() => {
    setIsLoading(true)
    Promise.all([fetchRules(), fetchGroups()]).finally(() => setIsLoading(false))
    
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [fetchRules, fetchGroups])

  // Real-time updates via WebSocket
  useWebSocketEvent(
    'workflow_event',
    useCallback(() => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
      debounceRef.current = window.setTimeout(() => {
        fetchRules()
        fetchGroups()
      }, 500)
    }, [fetchRules, fetchGroups]),
  )

  return {
    rules,
    groups,
    isLoading,
    ruleCount,
    enabledCount,
    eventTypes,
    sources,
    enforcementEnabled,
    fetchRules,
    fetchGroups,
    fetchRuleDetail,
    toggleRule,
    createRule,
    updateRule,
    deleteRule,
    installFromTemplate,
    setEnforcement,
    bulkToggleRules,
  }
}
