import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface WorkflowSummary {
  id: string
  name: string
  description: string | null
  workflow_type: string
  version: string
  enabled: boolean
  priority: number
  source: string
  sources: string[] | null
  tags: string[] | null
  project_id: string | null
  created_at: string
  updated_at: string
  deleted_at: string | null
}

export interface WorkflowDetail extends WorkflowSummary {
  definition_json: string
  canvas_json: string | null
}

function getBaseUrl(): string {
  return ''
}

export function useWorkflows() {
  const [workflows, setWorkflows] = useState<WorkflowDetail[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedWorkflow, setSelectedWorkflow] = useState<WorkflowDetail | null>(null)

  const fetchWorkflows = useCallback(async (params?: {
    workflow_type?: string
    enabled?: boolean
    project_id?: string
    include_deleted?: boolean
  }) => {
    try {
      const baseUrl = getBaseUrl()
      const searchParams = new URLSearchParams()
      if (params?.workflow_type) searchParams.set('workflow_type', params.workflow_type)
      if (params?.enabled !== undefined) searchParams.set('enabled', String(params.enabled))
      if (params?.project_id) searchParams.set('project_id', params.project_id)
      if (params?.include_deleted) searchParams.set('include_deleted', 'true')
      const query = searchParams.toString()
      const url = `${baseUrl}/api/workflows${query ? `?${query}` : ''}`

      const response = await fetch(url)
      if (response.ok) {
        const data = await response.json()
        setWorkflows(data.definitions || [])
      }
    } catch (e) {
      console.error('Failed to fetch workflows:', e)
    }
  }, [])

  const fetchWorkflow = useCallback(async (id: string): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}`)
      if (response.ok) {
        const data = await response.json()
        return data.definition || null
      }
    } catch (e) {
      console.error('Failed to fetch workflow:', e)
    }
    return null
  }, [])

  const createWorkflow = useCallback(async (params: {
    name: string
    definition_json: string
    workflow_type?: string
    description?: string
    priority?: number
    enabled?: boolean
    sources?: string[]
    tags?: string[]
  }): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return data.definition
        }
      }
    } catch (e) {
      console.error('Failed to create workflow:', e)
    }
    return null
  }, [fetchWorkflows])

  const updateWorkflow = useCallback(async (
    id: string,
    params: {
      name?: string
      definition_json?: string
      description?: string
      priority?: number
      enabled?: boolean
      sources?: string[]
      tags?: string[]
      canvas_json?: string
    },
  ): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return data.definition
        }
      }
    } catch (e) {
      console.error('Failed to update workflow:', e)
    }
    return null
  }, [fetchWorkflows])

  const deleteWorkflow = useCallback(async (id: string): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}`, {
        method: 'DELETE',
      })
      if (response.ok) {
        const data = await response.json()
        if (data.deleted) {
          if (selectedId === id) {
            setSelectedId(null)
            setSelectedWorkflow(null)
          }
          await fetchWorkflows()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to delete workflow:', e)
    }
    return false
  }, [fetchWorkflows, selectedId])

  const duplicateWorkflow = useCallback(async (
    id: string,
    newName: string,
  ): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/duplicate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_name: newName }),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return data.definition
        }
      }
    } catch (e) {
      console.error('Failed to duplicate workflow:', e)
    }
    return null
  }, [fetchWorkflows])

  const toggleEnabled = useCallback(async (id: string): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/toggle`, {
        method: 'PUT',
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return data.definition
        }
      }
    } catch (e) {
      console.error('Failed to toggle workflow:', e)
    }
    return null
  }, [fetchWorkflows])

  const importYaml = useCallback(async (
    yamlContent: string,
    projectId?: string,
  ): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ yaml_content: yamlContent, project_id: projectId }),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return data.definition
        }
      }
    } catch (e) {
      console.error('Failed to import workflow YAML:', e)
    }
    return null
  }, [fetchWorkflows])

  const exportYaml = useCallback(async (id: string): Promise<string | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/export`)
      if (response.ok) {
        return await response.text()
      }
    } catch (e) {
      console.error('Failed to export workflow YAML:', e)
    }
    return null
  }, [])

  const installFromTemplate = useCallback(async (id: string): Promise<WorkflowDetail | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/install`, {
        method: 'POST',
      })
      const data = await response.json().catch(() => ({}))
      if (response.ok && data.status === 'success') {
        await fetchWorkflows()
        return data.definition
      }
      window.alert(data.detail || 'Failed to install from template')
    } catch (e) {
      console.error('Failed to install from template:', e)
      window.alert(`Failed to install from template: ${e instanceof Error ? e.message : String(e)}`)
    }
    return null
  }, [fetchWorkflows])

  const installAllTemplates = useCallback(async (workflowType?: string): Promise<number> => {
    try {
      const baseUrl = getBaseUrl()
      const params = workflowType ? `?workflow_type=${encodeURIComponent(workflowType)}` : ''
      const response = await fetch(`${baseUrl}/api/workflows/install-all-templates${params}`, {
        method: 'POST',
      })
      const data = await response.json().catch(() => ({}))
      if (response.ok && data.status === 'success') {
        await fetchWorkflows()
        return data.count || 0
      }
      window.alert(data.detail || 'Failed to install all templates')
    } catch (e) {
      console.error('Failed to install all templates:', e)
      window.alert(`Failed: ${e instanceof Error ? e.message : String(e)}`)
    }
    return 0
  }, [fetchWorkflows])

  const restoreWorkflow = useCallback(async (id: string): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/workflows/${encodeURIComponent(id)}/restore`, {
        method: 'POST',
      })
      if (response.ok) {
        const data = await response.json()
        if (data.status === 'success') {
          await fetchWorkflows()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to restore workflow:', e)
    }
    return false
  }, [fetchWorkflows])

  // Select a workflow and fetch its details
  const selectWorkflow = useCallback(async (id: string | null) => {
    setSelectedId(id)
    if (id) {
      const detail = await fetchWorkflow(id)
      setSelectedWorkflow(detail)
    } else {
      setSelectedWorkflow(null)
    }
  }, [fetchWorkflow])

  // Computed values
  const workflowCount = useMemo(() => {
    return workflows.filter(w => w.workflow_type === 'workflow').length
  }, [workflows])

  const pipelineCount = useMemo(() => {
    return workflows.filter(w => w.workflow_type === 'pipeline').length
  }, [workflows])

  const activeCount = useMemo(() => {
    return workflows.filter(w => w.enabled).length
  }, [workflows])

  // Auto-fetch on mount
  useEffect(() => {
    setIsLoading(true)
    fetchWorkflows().finally(() => setIsLoading(false))
  }, [fetchWorkflows])

  // Real-time updates via WebSocket
  const debounceRef = useRef<number | null>(null)
  useEffect(() => {
    return () => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [])
  useWebSocketEvent(
    'workflow_event',
    useCallback(() => {
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
      debounceRef.current = window.setTimeout(() => fetchWorkflows(), 500)
    }, [fetchWorkflows]),
  )

  return {
    workflows,
    isLoading,
    selectedId,
    selectedWorkflow,
    workflowCount,
    pipelineCount,
    activeCount,
    fetchWorkflows,
    fetchWorkflow,
    createWorkflow,
    updateWorkflow,
    deleteWorkflow,
    duplicateWorkflow,
    toggleEnabled,
    importYaml,
    exportYaml,
    restoreWorkflow,
    selectWorkflow,
    installFromTemplate,
    installAllTemplates,
  }
}
