import { useState, useEffect } from 'react'
import type { ProjectWithStats } from '../../hooks/useProjects'
import { Heading } from '../shared/Heading'

const SUMMARY_CLS = 'max-w-[800px]'
const GRID_CLS = 'flex max-w-[900px] flex-col gap-5'
const STATS_ROW_CLS =
  'flex flex-wrap items-baseline gap-x-6 gap-y-2 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4'
const STAT_CLS = 'flex items-baseline gap-2 text-[length:var(--text-base)]'
const STAT_LABEL_CLS = 'text-[var(--text-muted)]'
const STAT_VALUE_CLS = 'font-semibold text-[var(--text-primary)] [font-variant-numeric:tabular-nums]'

const SECTION_CLS = 'rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-4'
const HEADING_CLS = 'm-0 mb-3 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const TWO_COL_CLS = 'grid gap-5 [grid-template-columns:1fr_1fr] max-sm:[grid-template-columns:1fr]'

const TASK_BAR_CLS = 'mb-3 flex h-2 gap-0.5 overflow-hidden rounded'
const TASK_SEGMENT_CLS = 'rounded-sm'
const TASK_LEGEND_CLS = 'flex flex-wrap gap-x-5 gap-y-3'
const TASK_LEGEND_ITEM_CLS = 'flex items-center gap-1.5 text-[length:var(--text-sm)]'
const TASK_GLYPH_CLS = 'inline-flex h-2.5 w-2.5 shrink-0 items-center justify-center'
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

type TaskStatus = keyof TaskStats

const TASK_STATUS_ORDER: readonly TaskStatus[] = [
  'open',
  'in_progress',
  'needs_review',
  'escalated',
  'closed',
  'review_approved',
]

const STATUS_COLORS: Record<TaskStatus, string> = {
  open: 'var(--accent)',
  in_progress: 'var(--color-info)',
  needs_review: 'var(--color-warning-foreground)',
  escalated: 'var(--color-error)',
  review_approved: 'var(--color-success-foreground)',
  closed: 'var(--text-muted)',
}

const STATUS_LABELS: Record<TaskStatus, string> = {
  open: 'Open',
  in_progress: 'In Progress',
  needs_review: 'Needs Review',
  escalated: 'Escalated',
  closed: 'Closed',
  review_approved: 'Approved',
}

function StatusGlyph({ status }: { status: TaskStatus }) {
  const common = {
    'aria-hidden': true,
    width: 10,
    height: 10,
    viewBox: '0 0 12 12',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  }
  switch (status) {
    case 'open':
      return <svg {...common}><circle cx="6" cy="6" r="4.25" /></svg>
    case 'in_progress':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.25" />
          <path d="M6 1.75 A4.25 4.25 0 0 1 6 10.25 Z" fill="currentColor" stroke="none" />
        </svg>
      )
    case 'needs_review':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.25" />
          <circle cx="6" cy="6" r="1.5" fill="currentColor" stroke="none" />
        </svg>
      )
    case 'escalated':
      return (
        <svg {...common}>
          <polygon points="6,1.5 10.75,10.25 1.25,10.25" />
          <line x1="6" y1="5" x2="6" y2="7.5" />
          <circle cx="6" cy="9" r="0.5" fill="currentColor" stroke="none" />
        </svg>
      )
    case 'review_approved':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.25" fill="currentColor" stroke="none" />
          <path d="M3.75 6.25 L5.25 7.75 L8.25 4.25" stroke="var(--bg-primary)" strokeWidth="1.5" />
        </svg>
      )
    case 'closed':
      return (
        <svg {...common}>
          <circle cx="6" cy="6" r="4.25" />
          <path d="M3.75 6.25 L5.25 7.75 L8.25 4.25" />
        </svg>
      )
    default:
      return <svg {...common}><circle cx="6" cy="6" r="4.25" fill="currentColor" stroke="none" /></svg>
  }
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
    ? TASK_STATUS_ORDER.filter(s => (taskStats[s] || 0) > 0)
    : []

  return (
    <div className={SUMMARY_CLS}>
      <div className={GRID_CLS}>
        <div className={STATS_ROW_CLS}>
          <div className={STAT_CLS}>
            <span className={STAT_LABEL_CLS}>Sessions</span>
            <span className={STAT_VALUE_CLS}>{project.session_count}</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_LABEL_CLS}>Open Tasks</span>
            <span className={STAT_VALUE_CLS}>{project.open_task_count}</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_LABEL_CLS}>Total Tasks</span>
            <span className={STAT_VALUE_CLS}>{taskTotal}</span>
          </div>
          <div className={STAT_CLS}>
            <span className={STAT_LABEL_CLS}>Last Activity</span>
            <span className={STAT_VALUE_CLS}>
              {project.last_activity_at
                ? new Date(project.last_activity_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
                : '—'}
            </span>
          </div>
        </div>

        {taskStats && activeStatuses.length > 0 && (
          <div className={SECTION_CLS}>
            <Heading level={3} className={HEADING_CLS}>Task Breakdown</Heading>
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
                  <span className={TASK_GLYPH_CLS} style={{ color: STATUS_COLORS[status] }}>
                    <StatusGlyph status={status} />
                  </span>
                  <span className={TASK_LEGEND_LABEL_CLS}>{STATUS_LABELS[status]}</span>
                  <span className={TASK_LEGEND_COUNT_CLS}>{taskStats[status] || 0}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className={TWO_COL_CLS}>
          <div className={SECTION_CLS}>
            <Heading level={3} className={HEADING_CLS}>Details</Heading>
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
            <Heading level={3} className={HEADING_CLS}>Integrations</Heading>
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

              <dt className={DT_CLS}>Linear Project</dt>
              <dd className={DD_CLS}>{project.linear_project_id || <span className={EMPTY_CLS}>Not linked</span>}</dd>
            </dl>
          </div>
        </div>
      </div>
    </div>
  )
}
