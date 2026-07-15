import { useState, useEffect, useCallback, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

// =============================================================================
// Types
// =============================================================================

export interface CronJob {
  id: string
  project_id: string
  name: string
  description: string | null
  schedule_type: 'cron' | 'interval' | 'once'
  cron_expr: string | null
  interval_seconds: number | null
  run_at: string | null
  timezone: string
  action_type: 'agent_spawn' | 'pipeline' | 'shell'
  action_config: Record<string, unknown>
  enabled: boolean
  next_run_at: string | null
  last_run_at: string | null
  last_status: string | null
  consecutive_failures: number
  created_at: string
  updated_at: string
}

export interface CronRun {
  id: string
  cron_job_id: string
  triggered_at: string
  started_at: string | null
  completed_at: string | null
  status: string
  output: string | null
  error: string | null
  agent_run_id: string | null
  pipeline_execution_id: string | null
  child: CronRunChild | null
  created_at: string
}

export interface CronRunChild {
  type: 'agent_run' | 'pipeline_execution'
  id: string
  status: string | null
  terminal: boolean
  missing: boolean
}

export interface CronJobFilters {
  enabled: boolean | null
  search: string
}

export interface CreateCronJobRequest {
  project_id: string
  name: string
  action_type: string
  action_config: Record<string, unknown>
  schedule_type?: string
  cron_expr?: string
  interval_seconds?: number
  run_at?: string
  timezone?: string
  description?: string
}

export interface UpdateCronJobRequest {
  name?: string
  description?: string
  schedule_type?: string
  cron_expr?: string
  interval_seconds?: number
  timezone?: string
  action_type?: string
  action_config?: Record<string, unknown>
  enabled?: boolean
}

// =============================================================================
// Helpers
// =============================================================================

const REFETCH_DEBOUNCE_MS = 500

function getBaseUrl(): string {
  return ''
}

// =============================================================================
// Hook
// =============================================================================

export function useCronJobs(projectId?: string | null) {
  const [jobs, setJobs] = useState<CronJob[]>([])
  const [selectedJob, setSelectedJob] = useState<CronJob | null>(null)
  const [runs, setRuns] = useState<CronRun[]>([])
  const [filters, setFilters] = useState<CronJobFilters>({
    enabled: null,
    search: '',
  })
  const [isLoading, setIsLoading] = useState(true)
  const [isRunsLoading, setIsRunsLoading] = useState(false)
  const debouncedRefetchRef = useRef<number | null>(null)
  const jobsRequestGenerationRef = useRef(0)
  const runsRequestGenerationRef = useRef(0)

  // Fetch jobs list
  const fetchJobs = useCallback(async () => {
    const requestGeneration = ++jobsRequestGenerationRef.current
    try {
      const baseUrl = getBaseUrl()
      const params = new URLSearchParams()
      if (filters.enabled !== null) params.set('enabled', String(filters.enabled))
      if (projectId) params.set('project_id', projectId)

      const response = await fetch(`${baseUrl}/api/cron/jobs?${params}`)
      if (response.ok) {
        const data = await response.json()
        if (requestGeneration === jobsRequestGenerationRef.current) {
          setJobs(data.jobs || [])
        }
      }
    } catch (e) {
      if (requestGeneration === jobsRequestGenerationRef.current) {
        console.error('Failed to fetch cron jobs:', e)
      }
    } finally {
      if (requestGeneration === jobsRequestGenerationRef.current) {
        setIsLoading(false)
      }
    }
  }, [filters.enabled, projectId])

  // Fetch runs for a job
  const fetchRuns = useCallback(async (jobId: string) => {
    const requestGeneration = ++runsRequestGenerationRef.current
    setIsRunsLoading(true)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs/${encodeURIComponent(jobId)}/runs?limit=20`)
      if (response.ok) {
        const data = await response.json()
        if (requestGeneration === runsRequestGenerationRef.current) {
          setRuns(data.runs || [])
        }
      }
    } catch (e) {
      if (requestGeneration === runsRequestGenerationRef.current) {
        console.error('Failed to fetch cron runs:', e)
      }
    } finally {
      if (requestGeneration === runsRequestGenerationRef.current) {
        setIsRunsLoading(false)
      }
    }
  }, [])

  // Create a job
  const createJob = useCallback(async (
    request: Omit<CreateCronJobRequest, 'project_id'>,
  ): Promise<CronJob | null> => {
    if (!projectId) {
      console.error('Cannot create cron job without a project ID')
      return null
    }

    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...request, project_id: projectId }),
      })
      if (response.ok) {
        const data = await response.json()
        const job = data.job as CronJob
        setJobs(prev => [job, ...prev])
        return job
      }
    } catch (e) {
      console.error('Failed to create cron job:', e)
    }
    return null
  }, [projectId])

  // Update a job
  const updateJob = useCallback(async (jobId: string, request: UpdateCronJobRequest): Promise<CronJob | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs/${encodeURIComponent(jobId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
      })
      if (response.ok) {
        const data = await response.json()
        const updated = data.job as CronJob
        setJobs(prev => prev.map(j => j.id === jobId ? updated : j))
        if (selectedJob?.id === jobId) setSelectedJob(updated)
        return updated
      }
    } catch (e) {
      console.error('Failed to update cron job:', e)
    }
    return null
  }, [selectedJob])

  // Delete a job
  const deleteJob = useCallback(async (jobId: string): Promise<boolean> => {
    if (!projectId || !jobs.some(job => job.id === jobId && job.project_id === projectId)) {
      return false
    }
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs/${encodeURIComponent(jobId)}`, {
        method: 'DELETE',
      })
      if (response.ok) {
        setJobs(prev => prev.filter(j => j.id !== jobId))
        if (selectedJob?.id === jobId) {
          setSelectedJob(null)
          setRuns([])
        }
        return true
      }
    } catch (e) {
      console.error('Failed to delete cron job:', e)
    }
    return false
  }, [jobs, projectId, selectedJob])

  // Toggle a job
  const toggleJob = useCallback(async (jobId: string): Promise<CronJob | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs/${encodeURIComponent(jobId)}/toggle`, {
        method: 'POST',
      })
      if (response.ok) {
        const data = await response.json()
        const updated = data.job as CronJob
        setJobs(prev => prev.map(j => j.id === jobId ? updated : j))
        if (selectedJob?.id === jobId) setSelectedJob(updated)
        return updated
      }
    } catch (e) {
      console.error('Failed to toggle cron job:', e)
    }
    return null
  }, [selectedJob])

  // Run a job immediately
  const runNow = useCallback(async (jobId: string): Promise<CronRun | null> => {
    if (!projectId || !jobs.some(job => job.id === jobId && job.project_id === projectId)) {
      return null
    }
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/cron/jobs/${encodeURIComponent(jobId)}/run`, {
        method: 'POST',
      })
      if (response.ok) {
        const data = await response.json()
        const run = data.run as CronRun
        setRuns(prev => [run, ...prev])
        return run
      }
    } catch (e) {
      console.error('Failed to run cron job:', e)
    }
    return null
  }, [jobs, projectId])

  // Select a job and load its runs
  const selectJob = useCallback((job: CronJob | null) => {
    setSelectedJob(job)
    if (job) {
      fetchRuns(job.id)
    } else {
      runsRequestGenerationRef.current += 1
      setRuns([])
      setIsRunsLoading(false)
    }
  }, [fetchRuns])

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
      jobsRequestGenerationRef.current += 1
      runsRequestGenerationRef.current += 1
    }
  }, [])

  // A project or server-side filter change invalidates the current detail selection.
  useEffect(() => {
    runsRequestGenerationRef.current += 1
    setSelectedJob(null)
    setRuns([])
    setIsRunsLoading(false)
  }, [projectId, filters.enabled])

  // Sync selectedJob with fresh data after fetchJobs updates jobs list
  useEffect(() => {
    if (selectedJob) {
      const updated = jobs.find(j => j.id === selectedJob.id)
      if (updated && JSON.stringify(updated) !== JSON.stringify(selectedJob)) {
        setSelectedJob(updated)
      }
    }
  }, [jobs]) // eslint-disable-line react-hooks/exhaustive-deps

  // Fetch on mount and when filters change
  useEffect(() => {
    setIsLoading(true)
    fetchJobs()
  }, [fetchJobs])

  // Real-time updates via WebSocket
  useWebSocketEvent(
    'cron_event',
    useCallback(() => {
      if (debouncedRefetchRef.current) window.clearTimeout(debouncedRefetchRef.current)
      debouncedRefetchRef.current = window.setTimeout(() => {
        fetchJobs()
        if (selectedJob) fetchRuns(selectedJob.id)
      }, REFETCH_DEBOUNCE_MS)
    }, [fetchJobs, fetchRuns, selectedJob]),
  )

  const refresh = useCallback(() => {
    setIsLoading(true)
    fetchJobs()
    if (selectedJob) fetchRuns(selectedJob.id)
  }, [fetchJobs, fetchRuns, selectedJob])

  // Client-side search filtering
  const filteredJobs = jobs.filter(j => {
    if (!filters.search) return true
    const q = filters.search.toLowerCase()
    return j.name.toLowerCase().includes(q) ||
      (j.description?.toLowerCase().includes(q) ?? false) ||
      j.action_type.toLowerCase().includes(q)
  })

  return {
    jobs: filteredJobs,
    selectedJob,
    selectJob,
    runs,
    filters,
    setFilters,
    isLoading,
    isRunsLoading,
    createJob,
    updateJob,
    deleteJob,
    toggleJob,
    runNow,
    refresh,
  }
}
