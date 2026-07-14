import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useCronJobs, type CronJob } from '../useCronJobs'
import { useWorkflows } from '../useWorkflows'

vi.mock('../useWebSocketEvent', () => ({
  useWebSocketEvent: () => undefined,
}))

interface DeferredResponse {
  promise: Promise<Response>
  resolve: (response: Response) => void
  reject: (error: Error) => void
}

function deferredResponse(): DeferredResponse {
  let resolve!: (response: Response) => void
  let reject!: (error: Error) => void
  const promise = new Promise<Response>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function jsonResponse(body: unknown): Response {
  return { ok: true, json: async () => body } as Response
}

function cronJob(id: string, projectId: string): CronJob {
  return {
    id,
    project_id: projectId,
    name: id,
    description: null,
    schedule_type: 'cron',
    cron_expr: '* * * * *',
    interval_seconds: null,
    run_at: null,
    timezone: 'UTC',
    action_type: 'pipeline',
    action_config: {},
    enabled: true,
    next_run_at: null,
    last_run_at: null,
    last_status: null,
    consecutive_failures: 0,
    created_at: '',
    updated_at: '',
  }
}

describe('selection fetch race protection', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('keeps the latest cron project list and run selection', async () => {
    const projectA = deferredResponse()
    const projectB = deferredResponse()
    const runsA = deferredResponse()
    const runsB = deferredResponse()
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (url.includes('/runs')) return url.includes('/job-a/') ? runsA.promise : runsB.promise
      return url.includes('project_id=project-a') ? projectA.promise : projectB.promise
    }))

    const { result, rerender } = renderHook(
      ({ projectId }) => useCronJobs(projectId),
      { initialProps: { projectId: 'project-a' } },
    )
    await act(async () => {
      projectA.resolve(jsonResponse({ jobs: [cronJob('job-a', 'project-a')] }))
    })
    act(() => result.current.selectJob(cronJob('job-a', 'project-a')))
    rerender({ projectId: 'project-b' })

    await act(async () => {
      projectB.resolve(jsonResponse({ jobs: [cronJob('job-b', 'project-b')] }))
    })
    expect(result.current.jobs.map(job => job.id)).toEqual(['job-b'])
    expect(result.current.selectedJob).toBeNull()

    act(() => result.current.selectJob(cronJob('job-b', 'project-b')))
    await act(async () => {
      runsB.resolve(jsonResponse({ runs: [{ id: 'run-b' }] }))
    })
    await act(async () => {
      runsA.reject(new Error('stale run failure'))
    })

    expect(result.current.runs.map(run => run.id)).toEqual(['run-b'])
    expect(result.current.isRunsLoading).toBe(false)
  })

  it('keeps the latest workflow filter results and detail selection', async () => {
    const initialList = deferredResponse()
    const filteredA = deferredResponse()
    const filteredB = deferredResponse()
    const detailA = deferredResponse()
    const detailB = deferredResponse()
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (url.endsWith('/api/workflows')) return initialList.promise
      if (url.includes('project_id=project-a')) return filteredA.promise
      if (url.includes('project_id=project-b')) return filteredB.promise
      if (url.endsWith('/workflow-a')) return detailA.promise
      return detailB.promise
    }))

    const { result } = renderHook(() => useWorkflows())
    await act(async () => {
      initialList.resolve(jsonResponse({ definitions: [] }))
    })
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    void result.current.fetchWorkflows({ project_id: 'project-a' })
    void result.current.fetchWorkflows({ project_id: 'project-b' })
    await act(async () => {
      filteredB.resolve(jsonResponse({ definitions: [{ id: 'workflow-b' }] }))
    })
    await act(async () => {
      filteredA.reject(new Error('stale list failure'))
    })
    expect(result.current.workflows.map(workflow => workflow.id)).toEqual(['workflow-b'])

    void result.current.selectWorkflow('workflow-a')
    void result.current.selectWorkflow('workflow-b')
    await act(async () => {
      detailB.resolve(jsonResponse({ definition: { id: 'workflow-b' } }))
    })
    await act(async () => {
      detailA.resolve(jsonResponse({ definition: { id: 'workflow-a' } }))
    })

    expect(result.current.selectedId).toBe('workflow-b')
    expect(result.current.selectedWorkflow?.id).toBe('workflow-b')
  })
})
