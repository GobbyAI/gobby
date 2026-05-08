import { useState, useEffect, useRef, useCallback } from 'react'
import type { WorktreeInfo } from '../../hooks/useSourceControl'

interface BranchInfo {
  name: string
  is_current: boolean
  is_remote: boolean
  worktree_id: string | null
}

interface BranchIndicatorProps {
  currentBranch: string | null
  worktreePath: string | null
  projectId: string | null
  onWorktreeChange: (worktreePath: string, worktreeId?: string) => void
  disabled?: boolean
  variant?: 'toolbar' | 'select'
  compact?: boolean
}

function truncateLabel(value: string, maxChars: number): string {
  return value.length > maxChars ? `${value.slice(0, maxChars)}...` : value
}

async function readCheckoutError(response: Response, branchName: string): Promise<string> {
  try {
    const data: unknown = await response.json()
    const detail = (data as { detail?: unknown } | null)?.detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
  } catch {
    // Fall through to status text.
  }
  return response.statusText || `Failed to switch to ${branchName}`
}

export function BranchIndicator({
  currentBranch,
  worktreePath,
  projectId,
  onWorktreeChange,
  variant = 'toolbar',
  disabled = false,
  compact = false,
}: BranchIndicatorProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [worktrees, setWorktrees] = useState<WorktreeInfo[]>([])
  const [branches, setBranches] = useState<BranchInfo[]>([])
  const [mainRepoPath, setMainRepoPath] = useState<string | null>(null)
  const [apiBranch, setApiBranch] = useState<string | null>(null)
  const [checkoutError, setCheckoutError] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const buildParams = useCallback(() => {
    const params = new URLSearchParams()
    if (projectId) params.set('project_id', projectId)
    return params
  }, [projectId])

  // Eagerly fetch current branch on mount / project change
  useEffect(() => {
    let stale = false
    fetch(`/api/source-control/status?${buildParams()}`)
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (stale || !data) return
        if (data.current_branch) setApiBranch(data.current_branch)
        if (data.repo_path) setMainRepoPath(data.repo_path)
      })
      .catch(() => {})
    return () => { stale = true }
  }, [buildParams])

  // Click-outside-close
  useEffect(() => {
    if (!isOpen) return
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [isOpen])

  // Fetch worktrees + branches when dropdown opens
  const fetchDropdownData = useCallback(async () => {
    const params = buildParams()
    const [wtRes, brRes, statusRes] = await Promise.allSettled([
      fetch(`/api/source-control/worktrees?${params}`),
      fetch(`/api/source-control/branches?${params}`),
      fetch(`/api/source-control/status?${params}`),
    ])

    if (wtRes.status === 'fulfilled' && wtRes.value.ok) {
      const data = await wtRes.value.json()
      setWorktrees(data.worktrees || [])
    }
    if (brRes.status === 'fulfilled' && brRes.value.ok) {
      const data = await brRes.value.json()
      setBranches((data.branches || []).filter((b: BranchInfo) => !b.is_remote))
    }
    if (statusRes.status === 'fulfilled' && statusRes.value.ok) {
      const data = await statusRes.value.json()
      if (data.repo_path) setMainRepoPath(data.repo_path)
      if (data.current_branch) setApiBranch(data.current_branch)
    }
  }, [buildParams])

  const handleToggle = () => {
    if (disabled) return
    if (!isOpen) {
      setCheckoutError(null)
      fetchDropdownData()
    }
    setIsOpen(!isOpen)
  }

  const handleSelectWorktree = (path: string, id?: string) => {
    setCheckoutError(null)
    onWorktreeChange(path, id)
    setIsOpen(false)
  }

  const handleSelectBranch = async (branchName: string) => {
    setCheckoutError(null)
    if (!mainRepoPath) {
      setCheckoutError('Repository path unavailable')
      return
    }

    const params = buildParams()
    try {
      const response = await fetch(`/api/source-control/branches/checkout?${params}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ branch_name: branchName }),
      })

      if (!response.ok) {
        setCheckoutError(await readCheckoutError(response, branchName))
        return
      }

      const data = await response.json()
      if (data.current_branch) setApiBranch(data.current_branch)
      onWorktreeChange(data.repo_path || mainRepoPath)
      setIsOpen(false)
    } catch {
      setCheckoutError(`Failed to switch to ${branchName}`)
    }
  }

  const effectiveBranch = currentBranch ?? apiBranch
  if (!effectiveBranch) return null

  const isDetached = effectiveBranch.startsWith('detached:')
  const rawDisplayBranch = isDetached ? effectiveBranch.replace('detached:', '') : effectiveBranch
  const displayBranch = compact ? truncateLabel(rawDisplayBranch, 10) : rawDisplayBranch

  // Branch names that have worktrees (avoid duplicates)
  const worktreeBranches = new Set(worktrees.map(wt => wt.branch_name))
  // Local branches without worktrees, excluding current
  const standaloneBranches = branches.filter(
    b => !b.is_remote && !worktreeBranches.has(b.name) && !b.is_current
  )

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={handleToggle}
        className={
          variant === 'select'
            ? `inline-flex h-9 min-w-0 shrink-0 items-center gap-1 rounded-md border border-border bg-transparent px-2.5 py-2 text-sm transition-colors ${
                disabled
                  ? 'cursor-not-allowed text-muted-foreground/50'
                  : 'text-muted-foreground hover:bg-muted hover:text-foreground'
              }`
            : `flex items-center gap-1 px-1.5 py-0.5 rounded text-xs transition-colors ${
                disabled
                  ? 'cursor-not-allowed text-muted-foreground/50'
                  : 'text-muted-foreground hover:bg-muted/60'
              }`
        }
        title={
          disabled
            ? 'Attached session owns branch and worktree'
            : worktreePath ?? 'Current branch'
        }
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        disabled={disabled}
      >
        <BranchIcon />
        <span className={isDetached ? 'italic' : ''}>
          {displayBranch}
        </span>
        <ChevronIcon />
      </button>

      {isOpen && (
        <div
          className="absolute right-0 bottom-full mb-1 w-64 rounded-md border border-border bg-background shadow-lg z-20 max-h-72 overflow-y-auto"
          role="listbox"
          aria-label="Switch branch or worktree"
        >
          {checkoutError && (
            <div role="alert" className="border-b border-border px-3 py-1.5 text-xs text-destructive">
              {checkoutError}
            </div>
          )}

          {/* Worktrees */}
          {worktrees.length > 0 && (
            <>
              <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted-foreground/50 border-b border-border">Worktrees</div>
              {worktrees.map((wt) => {
                const isActive = worktreePath === wt.worktree_path
                return (
                  <button
                    key={wt.id}
                    role="option"
                    aria-selected={isActive}
                    className={`w-full text-left px-3 py-1.5 text-xs hover:bg-muted flex items-center gap-2 ${
                      isActive ? 'bg-accent/20 text-accent' : ''
                    }`}
                    onClick={() => handleSelectWorktree(wt.worktree_path, wt.id)}
                    title={wt.worktree_path}
                  >
                    <WorktreeIcon />
                    <div className="min-w-0">
                      <div className="font-medium truncate">{wt.branch_name}</div>
                      <div className="text-muted-foreground/60 truncate text-[10px]">{wt.worktree_path}</div>
                    </div>
                  </button>
                )
              })}
            </>
          )}

          {/* Branches */}
          {standaloneBranches.length > 0 && (
            <>
              <div className="px-3 py-1 text-[10px] uppercase tracking-wider text-muted-foreground/50 border-b border-border">Branches</div>
              {standaloneBranches.map((b) => (
                <button
                  key={b.name}
                  role="option"
                  aria-selected={false}
                  className="w-full text-left px-3 py-1.5 text-xs hover:bg-muted flex items-center gap-2"
                  onClick={() => { void handleSelectBranch(b.name) }}
                >
                  <BranchIcon />
                  <span className="truncate">{b.name}</span>
                </button>
              ))}
            </>
          )}

          {worktrees.length === 0 && standaloneBranches.length === 0 && (
            <div className="px-3 py-2 text-xs text-muted-foreground">No other branches found</div>
          )}
        </div>
      )}
    </div>
  )
}

function BranchIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      <line x1="6" y1="3" x2="6" y2="15" />
      <circle cx="18" cy="6" r="3" />
      <circle cx="6" cy="18" r="3" />
      <path d="M18 9a9 9 0 0 1-9 9" />
    </svg>
  )
}

function WorktreeIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
    </svg>
  )
}

function ChevronIcon() {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0 opacity-50">
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}
