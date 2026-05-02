import { useState, useEffect } from 'react'
import type { ProjectWithStats } from '../../hooks/useProjects'

const SUMMARY_CLS = 'max-w-[800px]'
const GRID_CLS = 'flex max-w-[900px] flex-col gap-5'
const STATS_ROW_CLS =
  'grid gap-4 gap-x-6 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4 [grid-template-columns:repeat(auto-fit,minmax(80px,1fr))]'
const STAT_CLS = 'flex flex-col items-center gap-1'
const STAT_VALUE_CLS = 'font-[inherit] text-[length:var(--text-2xl)] font-semibold text-[var(--accent)]'
const STAT_LABEL_CLS = 'text-[length:var(--text-xs)] uppercase text-[var(--text-muted)]'

const SECTION_CLS = 'rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4'
const HEADING_CLS = 'm-0 mb-3 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const TWO_COL_CLS = 'grid gap-5 [grid-template-columns:1fr_1fr] max-sm:[grid-template-columns:1fr]'

const TASK_BAR_CLS = 'mb-3 flex h-2 gap-0.5 overflow-hidden rounded'
const TASK_SEGMENT_CLS = 'rounded-sm'
const TASK_LEGEND_CLS = 'flex flex-wrap gap-x-5 gap-y-3'
const TASK_LEGEND_ITEM_CLS = 'flex items-center gap-1.5 text-[length:var(--text-sm)]'
const TASK_DOT_CLS = 'h-2 w-2 shrink-0 rounded-full'
const TASK_LEGEND_LABEL_CLS = 'text-[var(--text-secondary)]'
const TASK_LEGEND_COUNT_CLS = 'font-mono font-semibold text-[var(--text-primary)]'

const DL_CLS =
  'm-0 grid gap-2 gap-x-4 [grid-template-columns:140px_1fr] max-sm:[grid-template-columns:1fr] max-sm:gap-y-1 max-sm:[&>dt:not(:first-of-type)]:mt-2'
const DT_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'
const DD_CLS = 'm-0 break-words text-[length:var(--text-base)] text-[var(--text-primary)]'
const EMPTY_CLS = 'italic text-[var(--text-muted)]'
const LINK_CLS = 'text-[var(--accent)] no-underline hover:underline'

interface ProjectSummaryProps {
  project: ProjectWithStats
}

interface TaskStats {
  open: number
  in_progress: number
  needs_review: number
  escalated: number
  closed: number
  review_approved: number
}

const STATUS_COLORS: Record<string, string> = {
  open: 'var(--accent)',
  in_progress: '#f59e0b',
  needs_review: '#8b5cf6',
  escalated: '#ef4444',
  closed: '#22c55e',
  review_approved: '#06b6d4',
}

const STATUS_LABELS: Record<string, string> = {
  open: 'Open',
  in_progress: 'In Progress',
  needs_review: 'Needs Review',
  escalated: 'Escalated',
  closed: 'Closed',
  review_approved: 'Approved',
}

export function ProjectSummary({ project }: ProjectSummaryProps) {
  const [taskStats, setTaskStats] = useState<TaskStats | null>(null)
  const [taskTotal, setTaskTotal] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    fetch(`/api/tasks?project_id=${project.id}&limit=0`, { signal: controller.signal })
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (!data) return
        setTaskStats(data.stats || null)
        setTaskTotal(data.total || 0)
      })
      .catch(e => { if (e.name !== 'AbortError') console.debug('Task stats fetch failed:', e) })
    return () => controller.abort()
  }, [project.id])

  const activeStatuses = taskStats
    ? (['open', 'in_progress', 'needs_review', 'escalated', 'closed', 'review_approved'] as const).filter(s => (taskStats[s] || 0) > 0)
    : []

  return (
    <div className={SUMMARY_CLS}>
      <div className={GRID_CLS}>
        <div className={STATS_ROW_CLS}>
          <div className={STAT_CLS}>
            <span className={STAT_VALUE_CLS}>{project.session_count}</span>
            <span className={STAT_LABEL_CLS}>Sessions</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_VALUE_CLS}>{project.open_task_count}</span>
            <span className={STAT_LABEL_CLS}>Open Tasks</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_VALUE_CLS}>{taskTotal}</span>
            <span className={STAT_LABEL_CLS}>Total Tasks</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_VALUE_CLS}>
              {project.last_activity_at
                ? new Date(project.last_activity_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                : '—'}
            </span>
            <span className={STAT_LABEL_CLS}>Last Activity</span>
          </div>
        </div>

        {taskStats && activeStatuses.length > 0 && (
          <div className={SECTION_CLS}>
            <h3 className={HEADING_CLS}>Task Breakdown</h3>
            <div className={TASK_BAR_CLS}>
              {activeStatuses.map(status => {
                const count = taskStats[status] || 0
                const pct = taskTotal > 0 ? (count / taskTotal) * 100 : 0
                return (
                  <div
                    key={status}
                    className={TASK_SEGMENT_CLS}
                    style={{ width: `${Math.max(pct, 2)}%`, backgroundColor: STATUS_COLORS[status] }}
                    title={`${STATUS_LABELS[status]}: ${count}`}
                  />
                )
              })}
            </div>
            <div className={TASK_LEGEND_CLS}>
              {activeStatuses.map(status => (
                <div key={status} className={TASK_LEGEND_ITEM_CLS}>
                  <span className={TASK_DOT_CLS} style={{ backgroundColor: STATUS_COLORS[status] }} />
                  <span className={TASK_LEGEND_LABEL_CLS}>{STATUS_LABELS[status]}</span>
                  <span className={TASK_LEGEND_COUNT_CLS}>{taskStats[status] || 0}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className={TWO_COL_CLS}>
          <div className={SECTION_CLS}>
            <h3 className={HEADING_CLS}>Details</h3>
            <dl className={DL_CLS}>
              <dt className={DT_CLS}>Name</dt>
              <dd className={DD_CLS}>{project.display_name}</dd>

              <dt className={DT_CLS}>Repository Path</dt>
              <dd className={DD_CLS}>{project.repo_path || <span className={EMPTY_CLS}>Not configured</span>}</dd>

              <dt className={DT_CLS}>Created</dt>
              <dd className={DD_CLS}>{new Date(project.created_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}</dd>

              <dt className={DT_CLS}>Last Updated</dt>
              <dd className={DD_CLS}>{new Date(project.updated_at).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}</dd>
            </dl>
          </div>

          <div className={SECTION_CLS}>
            <h3 className={HEADING_CLS}>Integrations</h3>
            <dl className={DL_CLS}>
              <dt className={DT_CLS}>GitHub</dt>
              <dd className={DD_CLS}>
                {project.github_url ? (
                  <a href={project.github_url} target="_blank" rel="noopener noreferrer" className={LINK_CLS}>
                    {project.github_repo || project.github_url}
                  </a>
                ) : (
                  <span className={EMPTY_CLS}>Not linked</span>
                )}
              </dd>

              <dt className={DT_CLS}>Linear Team</dt>
              <dd className={DD_CLS}>{project.linear_team_id || <span className={EMPTY_CLS}>Not linked</span>}</dd>
            </dl>
          </div>
        </div>
      </div>
    </div>
  )
}
