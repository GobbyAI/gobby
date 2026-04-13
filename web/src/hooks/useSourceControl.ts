import { useState, useEffect, useCallback, useRef } from 'react'

// =============================================================================
// Types
// =============================================================================

export interface SourceControlStatus {
  github_available: boolean
  github_repo: string | null
  current_branch: string | null
  branch_count: number
  worktree_count: number
  clone_count: number
}

export interface GitBranch {
  name: string
  is_current: boolean
  is_remote: boolean
  ahead: number
  behind: number
  last_commit_date: string
  worktree_id: string | null
}

export interface GitCommit {
  sha: string
  short_sha: string
  message: string
  author: string
  date: string
}

export interface PullRequest {
  number: number
  title: string
  state: 'open' | 'closed' | 'merged'
  author: string
  head_branch: string
  base_branch: string
  created_at: string
  updated_at: string
  draft: boolean
  checks_status: 'pending' | 'success' | 'failure' | null
  linked_task_id: string | null
}

export interface WorktreeInfo {
  id: string
  branch_name: string
  worktree_path: string
  status: string
  task_id: string | null
  agent_session_id: string | null
  project_id: string
  base_branch: string
  created_at: string
  updated_at: string
  merged_at: string | null
}

export interface CloneInfo {
  id: string
  branch_name: string
  clone_path: string
  remote_url: string | null
  status: string
  task_id: string | null
  project_id: string
  base_branch: string
  created_at: string
  updated_at: string
}

export interface CIWorkflowRun {
  id: number
  name: string
  status: string
  conclusion: string | null
  branch: string
  event: string
  created_at: string
  html_url: string
}

export interface Issue {
  number: number
  title: string
  state: 'open' | 'closed'
  author: string
  labels: { name: string; color: string }[]
  created_at: string
  updated_at: string
  comments: number
}

export interface IssueDetail {
  title: string
  body: string | null
  [key: string]: unknown
}

export interface DiffResult {
  diff_stat: string
  files: { status: string; path: string }[]
  patch: string
}

// =============================================================================
// Constants
// =============================================================================

const LOCAL_POLL_MS = 5000
const GITHUB_POLL_MS = 30000

function getBaseUrl(): string {
  return ''
}

// =============================================================================
// Hook
// =============================================================================

export function useSourceControl(projectId: string | null = null) {
  const [status, setStatus] = useState<SourceControlStatus | null>(null)
  const [branches, setBranches] = useState<GitBranch[]>([])
  const [prs, setPrs] = useState<PullRequest[]>([])
  const [worktrees, setWorktrees] = useState<WorktreeInfo[]>([])
  const [clones, setClones] = useState<CloneInfo[]>([])
  const [issues, setIssues] = useState<Issue[]>([])
  const [ciRuns, setCiRuns] = useState<CIWorkflowRun[]>([])

  const [isLoading, setIsLoading] = useState(true)
  const [errors, setErrors] = useState<Record<string, string>>({})

  const setFetcherError = useCallback((key: string, message: string | null) => {
    setErrors(prev => {
      if (message === null) {
        if (!(key in prev)) return prev
        const next = { ...prev }
        delete next[key]
        return next
      }
      return { ...prev, [key]: message }
    })
  }, [])

  const error = Object.values(errors).length > 0 ? Object.values(errors).join('; ') : null

  const localPollRef = useRef<number | null>(null)
  const githubPollRef = useRef<number | null>(null)
  const fetchLocalRef = useRef<() => Promise<void>>(() => Promise.resolve())
  const fetchGitHubRef = useRef<() => Promise<void>>(() => Promise.resolve())

  const buildParams = useCallback(
    (extra?: Record<string, string>) => {
      const params = new URLSearchParams()
      if (projectId) params.set('project_id', projectId)
      if (extra) {
        for (const [k, v] of Object.entries(extra)) params.set(k, v)
      }
      return params.toString()
    },
    [projectId]
  )

  // --- Fetch functions ---

  const fetchStatus = useCallback(async () => {
    try {
      const r = await fetch(`${getBaseUrl()}/api/source-control/status?${buildParams()}`)
      if (r.ok) {
        setStatus(await r.json())
        setFetcherError('status', null)
      } else {
        setFetcherError('status', `HTTP ${r.status}: ${r.statusText}`)
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setFetcherError('status', message)
      console.error('Failed to fetch source control status:', e)
    }
  }, [buildParams, setFetcherError])

  const fetchBranches = useCallback(async () => {
    try {
      const r = await fetch(`${getBaseUrl()}/api/source-control/branches?${buildParams()}`)
      if (r.ok) {
        const data = await r.json()
        setBranches(data.branches || [])
        setFetcherError('branches', null)
      } else {
        setFetcherError('branches', `Branches: HTTP ${r.status}`)
      }
    } catch (e) {
      setFetcherError('branches', e instanceof Error ? e.message : 'Failed to fetch branches')
      console.error('Failed to fetch branches:', e)
    }
  }, [buildParams, setFetcherError])

  const fetchPrs = useCallback(
    async (state = 'open') => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/prs?${buildParams({ state })}`
        )
        if (r.ok) {
          const data = await r.json()
          setPrs(data.prs || [])
          setFetcherError('prs', null)
        } else {
          setFetcherError('prs', `PRs: HTTP ${r.status}`)
        }
      } catch (e) {
        setFetcherError('prs', e instanceof Error ? e.message : 'Failed to fetch PRs')
        console.error('Failed to fetch PRs:', e)
      }
    },
    [buildParams, setFetcherError]
  )

  const fetchWorktrees = useCallback(async () => {
    try {
      const r = await fetch(`${getBaseUrl()}/api/source-control/worktrees?${buildParams()}`)
      if (r.ok) {
        const data = await r.json()
        setWorktrees(data.worktrees || [])
        setFetcherError('worktrees', null)
      } else {
        setFetcherError('worktrees', `Worktrees: HTTP ${r.status}`)
      }
    } catch (e) {
      setFetcherError('worktrees', e instanceof Error ? e.message : 'Failed to fetch worktrees')
      console.error('Failed to fetch worktrees:', e)
    }
  }, [buildParams, setFetcherError])

  const fetchClones = useCallback(async () => {
    try {
      const r = await fetch(`${getBaseUrl()}/api/source-control/clones?${buildParams()}`)
      if (r.ok) {
        const data = await r.json()
        setClones(data.clones || [])
        setFetcherError('clones', null)
      } else {
        setFetcherError('clones', `Clones: HTTP ${r.status}`)
      }
    } catch (e) {
      setFetcherError('clones', e instanceof Error ? e.message : 'Failed to fetch clones')
      console.error('Failed to fetch clones:', e)
    }
  }, [buildParams, setFetcherError])

  const fetchCiRuns = useCallback(async () => {
    try {
      const r = await fetch(`${getBaseUrl()}/api/source-control/cicd/runs?${buildParams()}`)
      if (r.ok) {
        const data = await r.json()
        setCiRuns(data.runs || [])
        setFetcherError('ciRuns', null)
      } else {
        setFetcherError('ciRuns', `CI runs: HTTP ${r.status}`)
      }
    } catch (e) {
      setFetcherError('ciRuns', e instanceof Error ? e.message : 'Failed to fetch CI/CD runs')
      console.error('Failed to fetch CI/CD runs:', e)
    }
  }, [buildParams, setFetcherError])

  const fetchIssues = useCallback(
    async (state = 'open') => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/issues?${buildParams({ state })}`
        )
        if (r.ok) {
          const data = await r.json()
          setIssues(data.issues || [])
          setFetcherError('issues', null)
        } else {
          setFetcherError('issues', `Issues: HTTP ${r.status}`)
        }
      } catch (e) {
        setFetcherError('issues', e instanceof Error ? e.message : 'Failed to fetch issues')
        console.error('Failed to fetch issues:', e)
      }
    },
    [buildParams, setFetcherError]
  )

  // --- On-demand fetchers ---

  const fetchCommits = useCallback(
    async (branchName: string, limit = 20): Promise<GitCommit[]> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/branches/${encodeURIComponent(branchName)}/commits?${buildParams({ limit: String(limit) })}`
        )
        if (r.ok) {
          const data = await r.json()
          return data.commits || []
        }
      } catch (e) {
        console.error('Failed to fetch commits:', e)
      }
      return []
    },
    [buildParams]
  )

  const fetchDiff = useCallback(
    async (base: string, head: string): Promise<DiffResult | null> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/diff?${buildParams({ base, head })}`
        )
        if (r.ok) return await r.json()
      } catch (e) {
        console.error('Failed to fetch diff:', e)
      }
      return null
    },
    [buildParams]
  )

  const fetchPrDetail = useCallback(
    async (number: number): Promise<Record<string, unknown> | null> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/prs/${number}?${buildParams()}`
        )
        if (r.ok) {
          const data = await r.json()
          return data.pr || null
        }
      } catch (e) {
        console.error('Failed to fetch PR detail:', e)
      }
      return null
    },
    [buildParams]
  )

  const fetchIssueDetail = useCallback(
    async (number: number): Promise<IssueDetail | null> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/issues/${number}?${buildParams()}`
        )
        if (r.ok) {
          const data = await r.json()
          return data.issue || null
        }
      } catch (e) {
        console.error('Failed to fetch issue detail:', e)
      }
      return null
    },
    [buildParams]
  )

  // --- Actions ---

  const deleteWorktree = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(`${getBaseUrl()}/api/source-control/worktrees/${id}`, {
          method: 'DELETE',
        })
        if (r.ok) {
          await fetchWorktrees()
          await fetchStatus()
          return true
        }
      } catch (e) {
        console.error('Failed to delete worktree:', e)
      }
      return false
    },
    [fetchWorktrees, fetchStatus]
  )

  const cleanupWorktrees = useCallback(
    async (hours = 24, dryRun = false): Promise<WorktreeInfo[]> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/worktrees/cleanup?${buildParams({
            hours: String(hours),
            dry_run: String(dryRun),
          })}`,
          { method: 'POST' }
        )
        if (r.ok) {
          const data = await r.json()
          if (!dryRun) {
            await fetchWorktrees()
            await fetchStatus()
          }
          return data.candidates || []
        }
      } catch (e) {
        console.error('Failed to cleanup worktrees:', e)
      }
      return []
    },
    [buildParams, fetchWorktrees, fetchStatus]
  )

  const syncWorktree = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(`${getBaseUrl()}/api/source-control/worktrees/${id}/sync`, {
          method: 'POST',
        })
        if (r.ok) {
          await fetchWorktrees()
          return true
        }
      } catch (e) {
        console.error('Failed to sync worktree:', e)
      }
      return false
    },
    [fetchWorktrees]
  )

  const deleteClone = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(`${getBaseUrl()}/api/source-control/clones/${id}`, {
          method: 'DELETE',
        })
        if (r.ok) {
          await fetchClones()
          await fetchStatus()
          return true
        }
      } catch (e) {
        console.error('Failed to delete clone:', e)
      }
      return false
    },
    [fetchClones, fetchStatus]
  )

  const syncClone = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(`${getBaseUrl()}/api/source-control/clones/${id}/sync`, {
          method: 'POST',
        })
        if (r.ok) {
          await fetchClones()
          return true
        }
      } catch (e) {
        console.error('Failed to sync clone:', e)
      }
      return false
    },
    [fetchClones]
  )

  // --- Fetch all local data ---

  const fetchLocal = useCallback(async () => {
    await Promise.all([fetchStatus(), fetchBranches(), fetchWorktrees(), fetchClones()])
    setIsLoading(false)
  }, [fetchStatus, fetchBranches, fetchWorktrees, fetchClones])

  // --- Fetch GitHub data ---

  const fetchGitHub = useCallback(async () => {
    await Promise.all([fetchPrs(), fetchIssues(), fetchCiRuns()])
  }, [fetchPrs, fetchIssues, fetchCiRuns])

  // --- Refresh all ---

  const refresh = useCallback(async () => {
    setIsLoading(true)
    await Promise.all([fetchLocal(), fetchGitHub()])
  }, [fetchLocal, fetchGitHub])

  // Keep refs updated with latest fetch functions
  useEffect(() => {
    fetchLocalRef.current = fetchLocal
    fetchGitHubRef.current = fetchGitHub
  }, [fetchLocal, fetchGitHub])

  // --- Effects ---

  // Local data: initial fetch + polling (5s) — restarts on projectId change
  useEffect(() => {
    let stale = false
    setIsLoading(true)
    setErrors({})
    if (!stale) fetchLocalRef.current()
    localPollRef.current = window.setInterval(() => { if (!stale) fetchLocalRef.current() }, LOCAL_POLL_MS)
    return () => {
      stale = true
      if (localPollRef.current) window.clearInterval(localPollRef.current)
    }
  }, [projectId])

  // GitHub data: initial fetch + polling (30s) — restarts on projectId change
  useEffect(() => {
    let stale = false
    if (!stale) fetchGitHubRef.current()
    githubPollRef.current = window.setInterval(() => { if (!stale) fetchGitHubRef.current() }, GITHUB_POLL_MS)
    return () => {
      stale = true
      if (githubPollRef.current) window.clearInterval(githubPollRef.current)
    }
  }, [projectId])

  return {
    // Data
    status,
    branches,
    prs,
    issues,
    worktrees,
    clones,
    ciRuns,

    // State
    isLoading,
    error,

    // On-demand
    fetchCommits,
    fetchDiff,
    fetchPrDetail,
    fetchPrs,
    fetchIssues,
    fetchIssueDetail,

    // Actions
    deleteWorktree,
    cleanupWorktrees,
    syncWorktree,
    deleteClone,
    syncClone,
    refresh,
  }
}
