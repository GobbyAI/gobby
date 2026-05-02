import { useMemo } from 'react'
import type { GobbyTaskDetail } from '../../hooks/useTasks'
import { getCanonicalTaskState, getTaskBucket, TASK_BUCKET_COLORS, TASK_BUCKET_LABELS } from '../../lib/taskState'

interface ResultSection {
  key: string
  label: string
  content: JSX.Element
}

const ROOT_CLS = 'flex flex-col gap-2'
const SECTION_CLS = 'flex flex-col gap-[0.2rem]'
const LABEL_CLS =
  'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] font-semibold uppercase tracking-[0.04em] text-[var(--text-muted)]'

const OUTCOME_CLS = 'flex items-center gap-2'
const OUTCOME_BADGE_CLS =
  'inline-flex items-center gap-[0.3rem] rounded-[12px] border px-2 py-[0.2rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] font-semibold'
const DATE_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

const VALIDATION_CLS = 'flex flex-col gap-[0.2rem]'
const VALIDATION_BADGE_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] font-semibold capitalize'
const VALIDATION_FEEDBACK_CLS =
  'rounded bg-[var(--bg-secondary)] px-2 py-[0.3rem] text-[length:calc(var(--font-size-base)*0.7)] leading-[1.4] text-[var(--text-secondary)]'

const COMMITS_CLS = 'flex flex-wrap gap-1'
const COMMIT_CLS =
  'inline-flex items-center gap-[0.3rem] rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-[0.4rem] py-[0.15rem] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-secondary)]'
const COMMIT_CODE_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] text-[var(--accent)]'
const COMMIT_TAG_CLS =
  'rounded-[2px] bg-[color-mix(in_srgb,var(--color-success-foreground)_10%,transparent)] px-[0.2rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.5)] font-semibold uppercase text-[var(--color-success-foreground)]'

const PR_CLS =
  'inline-flex w-fit items-center gap-[0.3rem] rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-[0.2rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--accent)] no-underline transition-colors duration-150 hover:border-[var(--accent)]'
const PR_REPO_CLS = 'text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

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
      case 'completed': return { text: 'Completed', color: 'var(--color-success-foreground)' }
      case 'duplicate': return { text: 'Duplicate', color: 'var(--text-muted)' }
      case 'wont_fix': return { text: "Won't Fix", color: 'var(--text-muted)' }
      case 'obsolete': return { text: 'Obsolete', color: 'var(--text-muted)' }
      case 'already_implemented': return { text: 'Already Done', color: 'var(--color-info)' }
      default: return { text: 'Closed', color: 'var(--color-success-foreground)' }
    }
  }
  if (state.is_escalated) {
    return { text: 'Escalated', color: 'var(--color-error)' }
  }
  if (bucket === 'blocked') {
    return { text: 'Blocked', color: TASK_BUCKET_COLORS.blocked }
  }
  return { text: TASK_BUCKET_LABELS[bucket], color: TASK_BUCKET_COLORS[bucket] }
}

const VALIDATION_COLORS: Record<string, string> = {
  passed: 'var(--color-success-foreground)',
  failed: 'var(--color-error)',
  skipped: 'var(--color-warning-foreground)',
  pending: 'var(--text-muted)',
}

interface TaskResultsProps {
  task: GobbyTaskDetail
}

export function TaskResults({ task }: TaskResultsProps) {
  const sections = useMemo(() => {
    const result: ResultSection[] = []
    const bucket = getTaskBucket(task)
    const state = getCanonicalTaskState(task)

    const isDone = bucket === 'closed' || bucket === 'merge_ready' || state.is_escalated
    if (isDone) {
      const outcome = outcomeLabel(task)
      const outcomeDate = task.closed_at || (bucket !== 'closed' ? task.updated_at : null)
      result.push({
        key: 'outcome',
        label: 'Outcome',
        content: (
          <div className={OUTCOME_CLS}>
            <span className={OUTCOME_BADGE_CLS} style={{ color: outcome.color, borderColor: outcome.color }}>
              <CheckIcon />
              {outcome.text}
            </span>
            {outcomeDate && (
              <span className={DATE_CLS}>{formatDate(outcomeDate)}</span>
            )}
          </div>
        ),
      })
    }

    if (task.validation_status && task.validation_status !== 'pending') {
      const vcolor = VALIDATION_COLORS[task.validation_status] || 'var(--text-muted)'
      result.push({
        key: 'validation',
        label: 'Validation',
        content: (
          <div className={VALIDATION_CLS}>
            <span className={VALIDATION_BADGE_CLS} style={{ color: vcolor }}>
              {task.validation_status}
            </span>
            {task.validation_feedback && (
              <div className={VALIDATION_FEEDBACK_CLS}>{task.validation_feedback}</div>
            )}
          </div>
        ),
      })
    }

    const allCommits = new Set<string>()
    if (task.closed_commit_sha) allCommits.add(task.closed_commit_sha)
    if (task.commits) task.commits.forEach(c => allCommits.add(c))

    if (allCommits.size > 0) {
      const commitList = Array.from(allCommits)
      result.push({
        key: 'commits',
        label: `Commits (${commitList.length})`,
        content: (
          <div className={COMMITS_CLS}>
            {commitList.map(sha => (
              <span key={sha} className={COMMIT_CLS}>
                <CommitIcon />
                <code className={COMMIT_CODE_CLS}>{sha.slice(0, 8)}</code>
                {sha === task.closed_commit_sha && (
                  <span className={COMMIT_TAG_CLS}>closing</span>
                )}
              </span>
            ))}
          </div>
        ),
      })
    }

    if (task.github_pr_number && task.github_repo) {
      result.push({
        key: 'pr',
        label: 'Pull Request',
        content: (
          <a
            className={PR_CLS}
            href={`https://github.com/${task.github_repo}/pull/${task.github_pr_number}`}
            target="_blank"
            rel="noopener noreferrer"
          >
            <PrIcon />
            <span>#{task.github_pr_number}</span>
            <span className={PR_REPO_CLS}>{task.github_repo}</span>
          </a>
        ),
      })
    }

    return result
  }, [task])

  if (sections.length === 0) return null

  return (
    <div className={ROOT_CLS}>
      {sections.map(section => (
        <div key={section.key} className={SECTION_CLS}>
          <span className={LABEL_CLS}>{section.label}</span>
          {section.content}
        </div>
      ))}
    </div>
  )
}
