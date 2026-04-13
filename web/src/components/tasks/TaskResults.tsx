import { useMemo } from 'react'
import type { GobbyTaskDetail } from '../../hooks/useTasks'
import { getCanonicalTaskState, getTaskBucket, TASK_BUCKET_COLORS, TASK_BUCKET_LABELS } from '../../lib/taskState'

// =============================================================================
// Types
// =============================================================================

interface ResultSection {
  key: string
  label: string
  content: JSX.Element
}

// =============================================================================
// Icons
// =============================================================================

function CommitIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="4" />
      <line x1="1.05" y1="12" x2="7" y2="12" />
      <line x1="17.01" y1="12" x2="22.96" y2="12" />
    </svg>
  )
}

function PrIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="18" cy="18" r="3" /><circle cx="6" cy="6" r="3" />
      <path d="M13 6h3a2 2 0 0 1 2 2v7" /><line x1="6" y1="9" x2="6" y2="21" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

// =============================================================================
// Helpers
// =============================================================================

function formatDate(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return 'Invalid date'
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function outcomeLabel(task: GobbyTaskDetail): { text: string; color: string } {
  const bucket = getTaskBucket(task)
  const state = getCanonicalTaskState(task)

  if (bucket === 'merge_ready') return { text: 'Approved', color: TASK_BUCKET_COLORS.merge_ready }
  if (bucket === 'closed') {
    switch (state.closed_reason) {
      case 'completed': return { text: 'Completed', color: '#22c55e' }
      case 'duplicate': return { text: 'Duplicate', color: '#737373' }
      case 'wont_fix': return { text: "Won't Fix", color: '#737373' }
      case 'obsolete': return { text: 'Obsolete', color: '#737373' }
      case 'already_implemented': return { text: 'Already Done', color: '#3b82f6' }
      default: return { text: 'Closed', color: '#22c55e' }
    }
  }
  if (state.is_escalated) {
    return { text: 'Escalated', color: '#f87171' }
  }
  if (bucket === 'blocked') {
    return { text: 'Blocked', color: TASK_BUCKET_COLORS.blocked }
  }
  return { text: TASK_BUCKET_LABELS[bucket], color: TASK_BUCKET_COLORS[bucket] }
}

const VALIDATION_COLORS: Record<string, string> = {
  passed: '#22c55e',
  failed: '#ef4444',
  skipped: '#eab308',
  pending: '#737373',
}

// =============================================================================
// TaskResults
// =============================================================================

interface TaskResultsProps {
  task: GobbyTaskDetail
}

export function TaskResults({ task }: TaskResultsProps) {
  const sections = useMemo(() => {
    const result: ResultSection[] = []
    const bucket = getTaskBucket(task)
    const state = getCanonicalTaskState(task)

    // Outcome summary
    const isDone = bucket === 'closed' || bucket === 'merge_ready' || state.is_escalated
    if (isDone) {
      const outcome = outcomeLabel(task)
      const outcomeDate = task.closed_at || (bucket !== 'closed' ? task.updated_at : null)
      result.push({
        key: 'outcome',
        label: 'Outcome',
        content: (
          <div className="task-results-outcome">
            <span className="task-results-outcome-badge" style={{ color: outcome.color, borderColor: outcome.color }}>
              <CheckIcon />
              {outcome.text}
            </span>
            {outcomeDate && (
              <span className="task-results-date">{formatDate(outcomeDate)}</span>
            )}
          </div>
        ),
      })
    }

    // Validation result
    if (task.validation_status && task.validation_status !== 'pending') {
      const vcolor = VALIDATION_COLORS[task.validation_status] || '#737373'
      result.push({
        key: 'validation',
        label: 'Validation',
        content: (
          <div className="task-results-validation">
            <span className="task-results-validation-badge" style={{ color: vcolor }}>
              {task.validation_status}
            </span>
            {task.validation_feedback && (
              <div className="task-results-validation-feedback">{task.validation_feedback}</div>
            )}
          </div>
        ),
      })
    }

    // Commits
    const allCommits = new Set<string>()
    if (task.closed_commit_sha) allCommits.add(task.closed_commit_sha)
    if (task.commits) task.commits.forEach(c => allCommits.add(c))

    if (allCommits.size > 0) {
      const commitList = Array.from(allCommits)
      result.push({
        key: 'commits',
        label: `Commits (${commitList.length})`,
        content: (
          <div className="task-results-commits">
            {commitList.map(sha => (
              <span key={sha} className="task-results-commit">
                <CommitIcon />
                <code>{sha.slice(0, 8)}</code>
                {sha === task.closed_commit_sha && (
                  <span className="task-results-commit-tag">closing</span>
                )}
              </span>
            ))}
          </div>
        ),
      })
    }

    // PR link
    if (task.github_pr_number && task.github_repo) {
      result.push({
        key: 'pr',
        label: 'Pull Request',
        content: (
          <a
            className="task-results-pr"
            href={`https://github.com/${task.github_repo}/pull/${task.github_pr_number}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            <PrIcon />
            <span>#{task.github_pr_number}</span>
            <span className="task-results-pr-repo">{task.github_repo}</span>
          </a>
        ),
      })
    }

    return result
  }, [task])

  if (sections.length === 0) return null

  return (
    <div className="task-results">
      {sections.map(section => (
        <div key={section.key} className="task-results-section">
          <span className="task-results-label">{section.label}</span>
          {section.content}
        </div>
      ))}
    </div>
  )
}
