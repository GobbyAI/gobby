import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
} from 'react'
import type { CronRun } from '../../hooks/useCronJobs'
import { cn } from '../../lib/utils'
import { cronRunStatusKind, type CronRunStatusKind } from '../activity/cronRunStatus'
import { ChevronIcon } from '../workflows/execution-utils'
import { formatDuration, formatRelativeTime } from './formatters'

const RUNS_TABLE_SCROLL_CLS = 'w-full overflow-x-auto'
const RUNS_TABLE_CLS =
  'w-full border-collapse text-[length:var(--text-sm)] max-sm:block [&_thead]:max-sm:hidden [&_tbody]:max-sm:block [&_tr]:max-sm:mb-2 [&_tr]:max-sm:block [&_tr]:max-sm:rounded-md [&_tr]:max-sm:border [&_tr]:max-sm:border-[var(--border)] [&_tr]:max-sm:bg-[var(--bg-secondary)] [&_tr]:max-sm:px-2.5 [&_tr]:max-sm:py-2 [&_td]:max-sm:block [&_td]:max-sm:border-b-0 [&_td]:max-sm:px-0 [&_td]:max-sm:py-1 [&_td]:max-sm:before:mb-0.5 [&_td]:max-sm:before:block [&_td]:max-sm:before:text-[length:var(--text-xs)] [&_td]:max-sm:before:uppercase [&_td]:max-sm:before:tracking-[0.5px] [&_td]:max-sm:before:text-[var(--text-secondary)] [&_td]:max-sm:before:[content:attr(data-label)]'
const RUNS_TH_CLS =
  'whitespace-nowrap border-b border-[var(--border)] px-2.5 py-1.5 text-left text-[length:var(--text-xs)] font-semibold uppercase text-[var(--text-secondary)]'
const RUNS_TD_CLS = 'border-b border-[var(--border)] px-2.5 py-1.5 [tr:last-child_&]:border-b-0'
const RUNS_OUTPUT_CLS =
  'max-w-[200px] overflow-hidden text-ellipsis whitespace-nowrap max-sm:max-w-none max-sm:whitespace-normal max-sm:overflow-visible max-sm:break-words'
const RUNS_EMPTY_CLS = 'p-4 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]'

const RUN_STATUS_CLS =
  'inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[length:var(--text-xs)] font-medium'
const RUN_STATUS_BG: Record<CronRunStatusKind, string> = {
  success:
    'bg-[color-mix(in_srgb,var(--color-success-foreground)_15%,transparent)] text-[var(--color-success-foreground)]',
  running: 'bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]',
  failure: 'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[var(--color-error)]',
  pending:
    'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]',
}

const RUN_ROW_CLS =
  'cursor-pointer transition-colors duration-100 hover:bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[var(--accent)]'
const RUN_ROW_EXPANDED_CLS = 'bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]'
const RUN_CHEVRON_CELL_CLS =
  'w-8 border-b border-[var(--border)] px-1 py-1.5 text-right text-[var(--text-secondary)] [tr:last-child_&]:border-b-0 max-sm:hidden'
const RUN_DETAILS_ROW_CLS = 'max-sm:block'
const RUN_DETAILS_CELL_CLS =
  'border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 pb-3 pt-1 [tr:last-child_&]:border-b-0 max-sm:block max-sm:px-0'
const RUN_DETAILS_INNER_CLS =
  'grid gap-2 transition-[grid-template-rows] duration-150 ease-[cubic-bezier(0.22,1,0.36,1)] motion-reduce:transition-none'
const RUN_DETAILS_PANEL_CLS = 'flex min-h-0 flex-col gap-2 overflow-hidden'
const RUN_DETAILS_PRE_WRAP_CLS = 'relative'
const RUN_DETAILS_PRE_CLS =
  'm-0 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3 pr-9 font-mono text-[length:var(--text-sm)] text-[var(--text-primary)]'
const RUN_DETAILS_PRE_EMPTY_CLS =
  'm-0 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-3 text-[length:var(--text-sm)] italic text-[var(--text-secondary)]'
const RUN_DETAILS_COPY_CLS =
  'absolute right-2 top-2 inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded border border-[var(--border)] bg-[var(--bg-primary)] text-[var(--text-secondary)] hover:text-[var(--text-primary)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]'
const RUN_DETAILS_META_CLS =
  'flex flex-wrap items-center gap-x-4 gap-y-1 text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const RUN_DETAILS_META_LABEL_CLS = 'uppercase tracking-[0.5px] text-[var(--text-muted)] mr-1'
const RUN_DETAILS_META_VALUE_CLS = 'font-mono text-[var(--text-primary)]'
const RUN_DETAILS_LINK_CLS =
  'inline-flex items-center gap-1 self-start rounded-sm text-[length:var(--text-sm)] text-[var(--accent)] hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-[var(--accent)]'

function formatTriggeredAbsolute(triggeredAt: string): string {
  const ts = Date.parse(triggeredAt)
  if (Number.isNaN(ts)) return triggeredAt
  return new Date(ts).toISOString()
}

function formatDurationMs(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt || !completedAt) return '—'
  const ms = Date.parse(completedAt) - Date.parse(startedAt)
  if (Number.isNaN(ms) || ms < 0) return '—'
  return `${ms}ms`
}

function shortenRunId(id: string): string {
  if (id.length <= 12) return id
  return `${id.slice(0, 8)}…${id.slice(-4)}`
}

interface CopyButtonProps {
  text: string
  label: string
}

function CopyButton({ text, label }: CopyButtonProps) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    }
  }, [])

  const onClick = useCallback(
    async (e: ReactMouseEvent) => {
      e.stopPropagation()
      try {
        await navigator.clipboard.writeText(text)
        setCopied(true)
        if (timerRef.current !== null) window.clearTimeout(timerRef.current)
        timerRef.current = window.setTimeout(() => setCopied(false), 1000)
      } catch {
        // Clipboard unavailable; silently fall back.
      }
    },
    [text],
  )

  return (
    <button
      type="button"
      className={RUN_DETAILS_COPY_CLS}
      onClick={onClick}
      aria-label={copied ? `${label} copied` : label}
      title={copied ? 'Copied' : label}
    >
      {copied ? (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  )
}

export interface RunHistoryTableProps {
  runs: CronRun[]
  isLoading: boolean
  onNavigateToPipelineExecution?: (executionId: string) => void
}

export function RunHistoryTable({
  runs,
  isLoading,
  onNavigateToPipelineExecution,
}: RunHistoryTableProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const toggleExpanded = useCallback((id: string) => {
    setExpandedId((prev) => (prev === id ? null : id))
  }, [])

  useEffect(() => {
    if (expandedId === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpandedId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [expandedId])

  if (isLoading) {
    return <div className={RUNS_EMPTY_CLS}>Loading runs...</div>
  }
  if (runs.length === 0) {
    return <div className={RUNS_EMPTY_CLS}>No runs yet</div>
  }

  return (
    <div className={RUNS_TABLE_SCROLL_CLS}>
      <table className={RUNS_TABLE_CLS}>
        <thead>
          <tr>
            <th className={RUNS_TH_CLS}>Triggered</th>
            <th className={RUNS_TH_CLS}>Status</th>
            <th className={RUNS_TH_CLS}>Duration</th>
            <th className={RUNS_TH_CLS}>Output</th>
            <th className={cn(RUNS_TH_CLS, 'w-8 max-sm:hidden')} aria-hidden="true" />
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const isExpanded = expandedId === run.id
            const detailsId = `run-details-${run.id}`
            const outputText = run.error || run.output || ''
            const hasOutput = outputText.length > 0
            const headerKeyDown = (e: ReactKeyboardEvent<HTMLTableRowElement>) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                toggleExpanded(run.id)
              }
            }
            return (
              <Fragment key={run.id}>
                <tr
                  role="button"
                  tabIndex={0}
                  aria-expanded={isExpanded}
                  aria-controls={detailsId}
                  className={cn(RUN_ROW_CLS, isExpanded && RUN_ROW_EXPANDED_CLS)}
                  onClick={() => toggleExpanded(run.id)}
                  onKeyDown={headerKeyDown}
                >
                  <td className={RUNS_TD_CLS} data-label="Triggered" title={run.triggered_at}>
                    {formatRelativeTime(run.triggered_at)}
                  </td>
                  <td className={RUNS_TD_CLS} data-label="Status">
                    <span className={cn(RUN_STATUS_CLS, RUN_STATUS_BG[cronRunStatusKind(run.status)])}>
                      {run.status}
                    </span>
                  </td>
                  <td className={RUNS_TD_CLS} data-label="Duration">
                    {formatDuration(run.started_at, run.completed_at)}
                  </td>
                  <td className={cn(RUNS_TD_CLS, RUNS_OUTPUT_CLS)} data-label="Output">
                    {outputText || '-'}
                  </td>
                  <td className={RUN_CHEVRON_CELL_CLS} aria-hidden="true">
                    <ChevronIcon expanded={isExpanded} />
                  </td>
                </tr>
                <tr className={RUN_DETAILS_ROW_CLS}>
                  <td
                    id={detailsId}
                    colSpan={5}
                    className={RUN_DETAILS_CELL_CLS}
                    data-label="Details"
                  >
                    <div
                      className={RUN_DETAILS_INNER_CLS}
                      style={{ gridTemplateRows: isExpanded ? '1fr' : '0fr' }}
                    >
                      <div className={RUN_DETAILS_PANEL_CLS}>
                        {hasOutput ? (
                          <div className={RUN_DETAILS_PRE_WRAP_CLS}>
                            <pre className={RUN_DETAILS_PRE_CLS}>{outputText}</pre>
                            <CopyButton text={outputText} label="Copy output" />
                          </div>
                        ) : (
                          <pre className={RUN_DETAILS_PRE_EMPTY_CLS}>No output recorded.</pre>
                        )}
                        <div className={RUN_DETAILS_META_CLS}>
                          <span>
                            <span className={RUN_DETAILS_META_LABEL_CLS}>Triggered</span>
                            <span className={RUN_DETAILS_META_VALUE_CLS}>
                              {formatTriggeredAbsolute(run.triggered_at)}
                            </span>
                          </span>
                          <span>
                            <span className={RUN_DETAILS_META_LABEL_CLS}>Duration</span>
                            <span className={RUN_DETAILS_META_VALUE_CLS}>
                              {formatDurationMs(run.started_at, run.completed_at)}
                            </span>
                          </span>
                          <span>
                            <span className={RUN_DETAILS_META_LABEL_CLS}>Run ID</span>
                            <span className={RUN_DETAILS_META_VALUE_CLS} title={run.id}>
                              {shortenRunId(run.id)}
                            </span>
                          </span>
                        </div>
                        {run.pipeline_execution_id && onNavigateToPipelineExecution && (
                          <button
                            type="button"
                            className={RUN_DETAILS_LINK_CLS}
                            onClick={(e) => {
                              e.stopPropagation()
                              onNavigateToPipelineExecution(run.pipeline_execution_id as string)
                            }}
                          >
                            View execution →
                          </button>
                        )}
                      </div>
                    </div>
                  </td>
                </tr>
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
