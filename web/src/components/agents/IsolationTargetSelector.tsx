import { useState, useEffect } from 'react'
import {
  AGENT_EDIT_FIELD_CLS,
  AGENT_EDIT_INPUT_CLS,
  AGENT_EDIT_LABEL_CLS,
} from './agents-styles'

interface WorktreeItem {
  id: string
  branch_name: string
  worktree_path: string
  status: string
}

interface CloneItem {
  id: string
  branch_name: string
  clone_path: string
  status?: string
}

interface IsolationTargetSelectorProps {
  isolation: string
  worktreeId: string | null
  cloneId: string | null
  onWorktreeIdChange: (id: string | null) => void
  onCloneIdChange: (id: string | null) => void
}

export function IsolationTargetSelector({ isolation, worktreeId, cloneId, onWorktreeIdChange, onCloneIdChange }: IsolationTargetSelectorProps) {
  const [worktrees, setWorktrees] = useState<WorktreeItem[]>([])
  const [clones, setClones] = useState<CloneItem[]>([])

  useEffect(() => {
    if (isolation === 'worktree') {
      fetch('/api/source-control/worktrees?status=active')
        .then(r => r.json())
        .then(data => setWorktrees(data.worktrees || []))
        .catch(() => setWorktrees([]))
    } else if (isolation === 'clone') {
      fetch('/api/source-control/clones')
        .then(r => r.json())
        .then(data => setClones(data.clones || []))
        .catch(() => setClones([]))
    }
  }, [isolation])

  if (isolation === 'worktree' && worktrees.length > 0) {
    return (
      <label className={AGENT_EDIT_FIELD_CLS}>
        <span className={AGENT_EDIT_LABEL_CLS}>Worktree</span>
        <select
          className={AGENT_EDIT_INPUT_CLS}
          value={worktreeId || ''}
          onChange={e => onWorktreeIdChange(e.target.value || null)}
        >
          <option value="">New worktree</option>
          {worktrees.map(wt => (
            <option key={wt.id} value={wt.id}>
              {wt.branch_name} ({wt.id.slice(0, 8)})
            </option>
          ))}
        </select>
      </label>
    )
  }

  if (isolation === 'clone' && clones.length > 0) {
    return (
      <label className={AGENT_EDIT_FIELD_CLS}>
        <span className={AGENT_EDIT_LABEL_CLS}>Clone</span>
        <select
          className={AGENT_EDIT_INPUT_CLS}
          value={cloneId || ''}
          onChange={e => onCloneIdChange(e.target.value || null)}
        >
          <option value="">New clone</option>
          {clones.map(c => (
            <option key={c.id} value={c.id}>
              {c.branch_name} ({c.id.slice(0, 8)})
            </option>
          ))}
        </select>
      </label>
    )
  }

  return null
}
