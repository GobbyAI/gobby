import { useState, useEffect, useCallback, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface PipelineStepExecution {
  id: number
  step_id: string
  status: 'pending' | 'running' | 'completed' | 'skipped' | 'failed' | 'waiting_approval'
  started_at: string | null
  completed_at: string | null
  output_json: string | null
  error: string | null
  approval_token: string | null
}

export interface PipelineExecutionRecord {
  id: string
  pipeline_name: string
  project_id: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'waiting_approval' | 'cancelled' | 'interrupted'
  created_at: string
  updated_at: string
  completed_at: string | null
  inputs_json: string | null
  outputs_json: string | null
  steps: PipelineStepExecution[]
}

interface Filters {
  status?: string
  pipeline_name?: string
}

export function usePipelineExecutions(projectId?: string) {
  const [executions, setExecutions] = useState<PipelineExecutionRecord[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [filters, setFilters] = useState<Filters>({})
  const refetchTimerRef = useRef<number | null>(null)

  const fetchExecutions = useCallback(async () => {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    if (filters.status) params.set('status', filters.status)
    if (filters.pipeline_name) params.set('pipeline_name', filters.pipeline_name)

    try {
      const res = await fetch(`/api/pipelines/executions?${params}`)
      if (res.ok) {
        const data = await res.json()
        setExecutions(data.executions || [])
      } else {
        console.error('Failed to fetch pipeline executions:', res.status, res.statusText)
        setExecutions([])
      }
    } catch (e) {
      console.error('Failed to fetch pipeline executions:', e)
    } finally {
      setIsLoading(false)
    }
  }, [projectId, filters])

  // Initial load + refetch on filter change
  useEffect(() => {
    setIsLoading(true)
    fetchExecutions()
  }, [fetchExecutions])

  // Real-time updates via singleton WebSocket
  useWebSocketEvent('pipeline_event', useCallback(() => {
    if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
    refetchTimerRef.current = window.setTimeout(() => {
      fetchExecutions()
    }, 500)
  }, [fetchExecutions]))

  // Clean up debounce timer on unmount
  useEffect(() => {
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current)
    }
  }, [])

  const approvePipeline = useCallback(async (token: string) => {
    const res = await fetch(`/api/pipelines/approve/${encodeURIComponent(token)}`, {
      method: 'POST',
    })
    if (!res.ok) throw new Error(`Failed to approve: ${res.statusText}`)
    const data = await res.json()
    fetchExecutions()
    return data
  }, [fetchExecutions])

  const rejectPipeline = useCallback(async (token: string) => {
    const res = await fetch(`/api/pipelines/reject/${encodeURIComponent(token)}`, {
      method: 'POST',
    })
    if (!res.ok) throw new Error(`Failed to reject: ${res.statusText}`)
    const data = await res.json()
    fetchExecutions()
    return data
  }, [fetchExecutions])

  return {
    executions,
    isLoading,
    filters,
    setFilters,
    fetchExecutions,
    approvePipeline,
    rejectPipeline,
  }
}
