import { useState, useMemo } from 'react'
import type { GitBranch, GitCommit, DiffResult, WorktreeInfo, CloneInfo } from '../../hooks/useSourceControl'
import { BranchDetail } from './BranchDetail'
import { ResourceCard } from './ResourceCard'
import { worktreeToFields, cloneToFields } from './resourceCardUtils'
import { Heading } from '../shared/Heading'

type Filter = 'all' | 'has_worktree' | 'has_clone' | 'local' | 'remote'

interface Props {
  branches: GitBranch[]
  worktrees: WorktreeInfo[]
  clones: CloneInfo[]
  currentBranch: string | null
  fetchCommits: (branch: string, limit?: number) => Promise<GitCommit[]>
  fetchDiff: (base: string, head: string) => Promise<DiffResult | null>
  onSyncWorktree: (id: string) => Promise<boolean>
  onDeleteWorktree: (id: string) => Promise<boolean>
  onSyncClone: (id: string) => Promise<boolean>
  onDeleteClone: (id: string) => Promise<boolean>
  onCleanupWorktrees: (hours?: number, dryRun?: boolean) => Promise<WorktreeInfo[]>
}

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'has_worktree', label: 'Has Worktree' },
  { key: 'has_clone', label: 'Has Clone' },
  { key: 'local', label: 'Local Only' },
  { key: 'remote', label: 'Remote Only' },
]

export function SourceControlView({
  branches, worktrees, clones, currentBranch,
  fetchCommits, fetchDiff,
  onSyncWorktree, onDeleteWorktree, onSyncClone, onDeleteClone,
  onCleanupWorktrees,
}: Props) {
  const [filter, setFilter] = useState<Filter>('all')
  const [selectedBranch, setSelectedBranch] = useState<string | null>(null)
  const [showRemote, setShowRemote] = useState(false)
  const [confirmCleanup, setConfirmCleanup] = useState(false)
  const [cleanupHours, setCleanupHours] = useState(24)
  const [cleanupLoading, setCleanupLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  // Build lookup maps: branch_name → worktrees/clones
  const wtByBranch = useMemo(() => {
    const map = new Map<string, WorktreeInfo[]>()
    for (const wt of worktrees) {
      const list = map.get(wt.branch_name) || []
      list.push(wt)
      map.set(wt.branch_name, list)
    }
    return map
  }, [worktrees])

  const cloneByBranch = useMemo(() => {
    const map = new Map<string, CloneInfo[]>()
    for (const c of clones) {
      const list = map.get(c.branch_name) || []
      list.push(c)
      map.set(c.branch_name, list)
    }
    return map
  }, [clones])

  // Branches that have a worktree or clone (for filter counts)
  const branchNames = useMemo(() => new Set(branches.map((b) => b.name)), [branches])

  // Orphans: worktrees/clones whose branch no longer exists
  const orphanWorktrees = useMemo(() => worktrees.filter((wt) => !branchNames.has(wt.branch_name)), [worktrees, branchNames])
  const orphanClones = useMemo(() => clones.filter((c) => !branchNames.has(c.branch_name)), [clones, branchNames])
  const hasOrphans = orphanWorktrees.length > 0 || orphanClones.length > 0

  // Apply filters
  const filteredBranches = useMemo(() => {
    let list = branches
    switch (filter) {
      case 'has_worktree':
        list = list.filter((b) => wtByBranch.has(b.name))
        break
      case 'has_clone':
        list = list.filter((b) => cloneByBranch.has(b.name))
        break
      case 'local':
        list = list.filter((b) => !b.is_remote)
        break
      case 'remote':
        list = list.filter((b) => b.is_remote)
        break
    }
    return list
  }, [branches, filter, wtByBranch, cloneByBranch])

  // Split local/remote when not using local/remote filter
  const localBranches = filteredBranches.filter((b) => !b.is_remote)
  const remoteBranches = filteredBranches.filter((b) => b.is_remote)
  const showSplit = filter !== 'local' && filter !== 'remote'

  // Filter counts
  const filterCounts = useMemo(() => ({
    all: branches.length,
    has_worktree: branches.filter((b) => wtByBranch.has(b.name)).length,
    has_clone: branches.filter((b) => cloneByBranch.has(b.name)).length,
    local: branches.filter((b) => !b.is_remote).length,
    remote: branches.filter((b) => b.is_remote).length,
  }), [branches, wtByBranch, cloneByBranch])

  // Selected branch resources
  const selectedWorktrees = selectedBranch ? wtByBranch.get(selectedBranch) || [] : []
  const selectedClones = selectedBranch ? cloneByBranch.get(selectedBranch) || [] : []

  const handleCleanup = async () => {
    setCleanupLoading(true)
    setActionError(null)
    try {
      await onCleanupWorktrees(cleanupHours, false)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : 'Failed to cleanup worktrees')
    } finally {
      setCleanupLoading(false)
      setConfirmCleanup(false)
    }
  }

  const handleResourceAction = async (action: (id: string) => Promise<boolean>, id: string, errorMsg: string) => {
    setActionError(null)
    try {
      await action(id)
    } catch (e) {
      setActionError(e instanceof Error ? e.message : errorMsg)
    }
  }

  const renderBranchRow = (b: GitBranch) => {
    const hasWt = wtByBranch.has(b.name)
    const hasClone = cloneByBranch.has(b.name)
    return (
      <tr
        key={b.name}
        className={`sc-table__row ${b.is_current ? 'sc-table__row--current' : ''} ${selectedBranch === b.name ? 'sc-table__row--selected' : ''}`}
        onClick={() => setSelectedBranch(selectedBranch === b.name ? null : b.name)}
        tabIndex={0}
        role="button"
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelectedBranch(selectedBranch === b.name ? null : b.name) } }}
      >
        <td className="sc-table__cell--name">
          {b.is_current && <span className="sc-branches__current-dot" />}
          {b.is_remote ? `origin/${b.name}` : b.name}
        </td>
        <td>
          {b.ahead > 0 && <span className="sc-branches__ahead">+{b.ahead}</span>}
          {b.behind > 0 && <span className="sc-branches__behind">-{b.behind}</span>}
          {b.ahead === 0 && b.behind === 0 && !b.is_remote && <span className="sc-text-muted">even</span>}
        </td>
        <td className="sc-table__cell--resources">
          {hasWt && <span className="sc-badge sc-badge--sm sc-badge--blue">worktree</span>}
          {hasClone && <span className="sc-badge sc-badge--sm sc-badge--accent">clone</span>}
        </td>
        <td className="sc-text-muted">
          {b.last_commit_date ? new Date(b.last_commit_date).toLocaleDateString() : ''}
        </td>
      </tr>
    )
  }

  return (
    <div className="sc-branches">
      {actionError && (
        <p className="sc-text-muted" style={{ color: 'var(--color-error)', padding: '8px 0' }}>{actionError}</p>
      )}

      <div className="sc-worktrees__toolbar">
        <div className="sc-filter-chips">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`sc-filter-chip ${filter === f.key ? 'sc-filter-chip--active' : ''}`}
              onClick={() => setFilter(filter === f.key ? 'all' : f.key)}
            >
              {f.label} ({filterCounts[f.key]})
            </button>
          ))}
        </div>
        <div className="sc-worktrees__actions">
          {confirmCleanup ? (
            <div className="sc-worktrees__confirm">
              <span className="sc-text-muted">Cleanup worktrees older than</span>
              <input
                type="number"
                min={1}
                value={cleanupHours}
                onChange={(e) => setCleanupHours(Math.max(1, Number(e.target.value)))}
                className="sc-input sc-input--sm"
                style={{ width: '4em' }}
                aria-label="Cleanup threshold in hours"
              />
              <span className="sc-text-muted">hours?</span>
              <button
                className="sc-btn sc-btn--sm sc-btn--danger"
                onClick={handleCleanup}
                disabled={cleanupLoading}
              >
                {cleanupLoading ? 'Cleaning...' : 'Confirm'}
              </button>
              <button className="sc-btn sc-btn--sm" onClick={() => setConfirmCleanup(false)}>
                Cancel
              </button>
            </div>
          ) : (
            worktrees.length > 0 && (
              <button className="sc-btn sc-btn--sm" onClick={() => setConfirmCleanup(true)}>
                Cleanup Stale
              </button>
            )
          )}
        </div>
      </div>

      <div className="sc-branches__main">
        <table className="sc-table">
          <colgroup>
            <col className="sc-col--branch" />
            <col className="sc-col--ahead-behind" />
            <col className="sc-col--resources" />
            <col className="sc-col--last-commit" />
          </colgroup>
          <thead>
            <tr>
              <th>Branch</th>
              <th>Ahead / Behind</th>
              <th>Resources</th>
              <th>Last Commit</th>
            </tr>
          </thead>
          <tbody>
            {(showSplit ? localBranches : filteredBranches).map(renderBranchRow)}
          </tbody>
        </table>

        {showSplit && remoteBranches.length > 0 && (
          <>
            <button
              className="sc-branches__toggle-remote"
              onClick={() => setShowRemote(!showRemote)}
            >
              {showRemote ? 'Hide' : 'Show'} remote branches ({remoteBranches.length})
            </button>
            {showRemote && (
              <table className="sc-table sc-table--remote">
                <colgroup>
                  <col className="sc-col--branch" />
                  <col className="sc-col--ahead-behind" />
                  <col className="sc-col--resources" />
                  <col className="sc-col--last-commit" />
                </colgroup>
                <thead>
                  <tr>
                    <th>Branch</th>
                    <th>Ahead / Behind</th>
                    <th>Resources</th>
                    <th>Last Commit</th>
                  </tr>
                </thead>
                <tbody>
                  {remoteBranches.map(renderBranchRow)}
                </tbody>
              </table>
            )}
          </>
        )}

        {hasOrphans && (
          <details className="sc-source-control__orphans">
            <summary className="sc-source-control__orphans-toggle">
              Orphaned resources ({orphanWorktrees.length + orphanClones.length})
            </summary>
            <div className="sc-card-grid">
              {orphanWorktrees.map((wt) => (
                <ResourceCard
                  key={`wt-${wt.id}`}
                  id={wt.id}
                  title={wt.branch_name}
                  status={wt.status}
                  fields={worktreeToFields(wt)}
                  onSync={(id) => handleResourceAction(onSyncWorktree, id, 'Failed to sync worktree')}
                  onDelete={(id) => handleResourceAction(onDeleteWorktree, id, 'Failed to delete worktree')}
                />
              ))}
              {orphanClones.map((c) => (
                <ResourceCard
                  key={`clone-${c.id}`}
                  id={c.id}
                  title={c.branch_name}
                  status={c.status}
                  fields={cloneToFields(c)}
                  onSync={(id) => handleResourceAction(onSyncClone, id, 'Failed to sync clone')}
                  onDelete={(id) => handleResourceAction(onDeleteClone, id, 'Failed to delete clone')}
                />
              ))}
            </div>
          </details>
        )}
      </div>

      {selectedBranch && (
        <BranchDetail
          branchName={selectedBranch}
          currentBranch={currentBranch}
          fetchCommits={fetchCommits}
          fetchDiff={fetchDiff}
          onClose={() => setSelectedBranch(null)}
        >
          {selectedWorktrees.length > 0 && (
            <div className="sc-detail-panel__section">
              <Heading level={4} className="sc-detail-panel__subtitle">Worktrees</Heading>
              <div className="sc-detail-panel__cards">
                {selectedWorktrees.map((wt) => (
                  <ResourceCard
                    key={wt.id}
                    id={wt.id}
                    title={wt.branch_name}
                    status={wt.status}
                    fields={worktreeToFields(wt)}
                    onSync={(id) => handleResourceAction(onSyncWorktree, id, 'Failed to sync worktree')}
                    onDelete={(id) => handleResourceAction(onDeleteWorktree, id, 'Failed to delete worktree')}
                  />
                ))}
              </div>
            </div>
          )}

          {selectedClones.length > 0 && (
            <div className="sc-detail-panel__section">
              <Heading level={4} className="sc-detail-panel__subtitle">Clones</Heading>
              <div className="sc-detail-panel__cards">
                {selectedClones.map((c) => (
                  <ResourceCard
                    key={c.id}
                    id={c.id}
                    title={c.branch_name}
                    status={c.status}
                    fields={cloneToFields(c)}
                    onSync={(id) => handleResourceAction(onSyncClone, id, 'Failed to sync clone')}
                    onDelete={(id) => handleResourceAction(onDeleteClone, id, 'Failed to delete clone')}
                  />
                ))}
              </div>
            </div>
          )}
        </BranchDetail>
      )}
    </div>
  )
}
