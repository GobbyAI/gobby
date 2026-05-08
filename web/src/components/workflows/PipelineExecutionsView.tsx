import { useState } from 'react'
import type { PipelineExecutionRecord } from '../../hooks/usePipelineExecutions'
import {
  StatusBadge,
  StepDisplay,
  ChevronIcon,
  AlertIcon,
  PipelineIcon,
  TraceIcon,
  PIPELINE_BTN_CLS,
  PIPELINE_BTN_APPROVE_CLS,
  PIPELINE_BTN_REJECT_CLS,
  PIPELINE_APPROVAL_CLS,
  PIPELINE_APPROVAL_MESSAGE_CLS,
  PIPELINE_APPROVAL_ACTIONS_CLS,
  PIPELINE_ERROR_CLS,
  PIPELINE_STEPS_CLS,
} from './execution-utils'
import { formatTime, formatDuration, formatJson } from './executionFormatters'
import { WORKFLOWS_CONTENT_CLS, WORKFLOWS_LOADING_CLS } from './workflows-styles'

const PAGINATION_FOOTER_CLS =
  'flex items-center justify-between px-3 py-2.5 mt-2 border-t border-border text-base text-[var(--text-secondary)]'
const PAGINATION_RIBBON_CLS = 'tabular-nums'
const PAGINATION_BUTTONS_CLS = 'flex gap-2'

const FILTERS_ROW_CLS = 'flex flex-wrap gap-1.5 mb-3'
const FILTER_CHIP_CLS =
  'px-2.5 py-1 border border-border rounded-full bg-transparent text-[var(--text-secondary)] text-[length:calc(var(--font-size-base)*0.75)] font-medium cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const FILTER_CHIP_ACTIVE_CLS =
  'bg-[var(--bg-tertiary)] text-[var(--text-primary)] border-[var(--text-muted)]'

const PANEL_CLS =
  'bg-[var(--bg-secondary)] border border-border rounded-lg overflow-hidden'
const PANEL_EMPTY_CLS = 'flex items-center justify-center min-h-[120px]'
const EMPTY_INNER_CLS = 'text-center text-[var(--text-muted)]'
const EMPTY_TEXT_CLS = 'text-[length:calc(var(--font-size-base)*0.875)]'

const LIST_CLS = 'max-h-[400px] overflow-y-auto'
const EXEC_ROW_CLS = 'border-b border-border last:border-b-0'
const EXEC_HEADER_CLS =
  'flex items-center justify-between px-4 py-3 cursor-pointer transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const EXEC_INFO_CLS = 'flex items-center gap-2'
const EXEC_META_CLS = 'flex items-center gap-2'
const EXEC_DETAILS_CLS = 'px-4 pb-4'

const PIPELINE_NAME_CLS =
  'font-medium text-[length:calc(var(--font-size-base)*0.9)]'
const PIPELINE_ID_CLS =
  'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] font-[inherit]'
const PIPELINE_TIME_CLS =
  'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const PIPELINE_STEP_TIMING_CLS =
  'text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] font-[inherit] tabular-nums'

const OUTPUTS_CLS = 'mt-3'
const OUTPUTS_HEADING_CLS =
  'text-[length:calc(var(--font-size-base)*0.8)] font-medium text-[var(--text-secondary)] mb-2'
const OUTPUTS_PRE_CLS =
  'font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] bg-[var(--code-bg)] p-3 rounded-md overflow-x-auto m-0'

interface PipelineExecutionsViewProps {
  executions: PipelineExecutionRecord[]
  isLoading: boolean
  filters: { status?: string; pipeline_name?: string }
  onFiltersChange: (filters: { status?: string; pipeline_name?: string }) => void
  onApprove: (token: string) => Promise<unknown>
  onReject: (token: string) => Promise<unknown>
  onNavigateToTrace?: (traceId: string) => void
  // Pagination (optional — when omitted the footer is hidden, preserving
  // the legacy single-page behavior for callers that don't track offset).
  total?: number
  limit?: number
  offset?: number
  onOffsetChange?: (offset: number) => void
}

interface PaginationFooterProps {
  total: number
  limit: number
  offset: number
  onOffsetChange: (offset: number) => void
}

function PaginationFooter({
  total,
  limit,
  offset,
  onOffsetChange,
}: PaginationFooterProps) {
  if (total <= 0) return null
  const start = offset + 1
  const end = Math.min(offset + limit, total)
  const hasPrev = offset > 0
  const hasNext = end < total
  return (
    <div className={PAGINATION_FOOTER_CLS}>
      <span className={PAGINATION_RIBBON_CLS}>
        {start}–{end} of {total}
      </span>
      <div className={PAGINATION_BUTTONS_CLS}>
        <button
          type="button"
          className={PIPELINE_BTN_CLS}
          disabled={!hasPrev}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Prev
        </button>
        <button
          type="button"
          className={PIPELINE_BTN_CLS}
          disabled={!hasNext}
          onClick={() => onOffsetChange(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  )
}

const STATUS_FILTERS = [
  { value: '', label: 'All' },
  { value: 'running', label: 'Running' },
  { value: 'waiting_approval', label: 'Waiting' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
]

export function PipelineExecutionsView({
  executions,
  isLoading,
  filters,
  onFiltersChange,
  onApprove,
  onReject,
  onNavigateToTrace,
  total,
  limit,
  offset,
  onOffsetChange,
}: PipelineExecutionsViewProps) {
  const showPagination =
    typeof total === 'number' &&
    typeof limit === 'number' &&
    typeof offset === 'number' &&
    typeof onOffsetChange === 'function'
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [actionLoading, setActionLoading] = useState<string | null>(null)

  const toggleExpanded = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleApprove = async (token: string) => {
    setActionLoading(token)
    try {
      await onApprove(token)
    } finally {
      setActionLoading(null)
    }
  }

  const handleReject = async (token: string) => {
    setActionLoading(token)
    try {
      await onReject(token)
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <div className={WORKFLOWS_CONTENT_CLS}>
      {/* Status filter chips */}
      <div className={FILTERS_ROW_CLS}>
        {STATUS_FILTERS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            className={`${FILTER_CHIP_CLS} ${(filters.status || '') === value ? FILTER_CHIP_ACTIVE_CLS : ''}`}
            onClick={() => onFiltersChange({ ...filters, status: value || undefined })}
          >
            {label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className={WORKFLOWS_LOADING_CLS}>Loading executions...</div>
      ) : executions.length === 0 ? (
        <div className={`${PANEL_CLS} ${PANEL_EMPTY_CLS}`}>
          <div className={EMPTY_INNER_CLS}>
            <PipelineIcon className="mb-2 opacity-50 inline-block" />
            <p className={EMPTY_TEXT_CLS}>No pipeline executions{filters.status ? ` with status "${filters.status}"` : ''}</p>
          </div>
        </div>
      ) : (
        <div className={PANEL_CLS}>
          <div className={LIST_CLS} style={{ maxHeight: 'none' }}>
            {executions.map((execution) => (
              <div key={execution.id} className={EXEC_ROW_CLS}>
                <div
                  className={EXEC_HEADER_CLS}
                  role="button"
                  tabIndex={0}
                  onClick={() => toggleExpanded(execution.id)}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpanded(execution.id) } }}
                >
                  <div className={EXEC_INFO_CLS}>
                    <StatusBadge status={execution.status} />
                    <span className={PIPELINE_NAME_CLS}>{execution.pipeline_name}</span>
                    <span className={PIPELINE_ID_CLS}>{execution.id.slice(0, 12)}</span>
                  </div>
                  <div className={EXEC_META_CLS}>
                    <span className={PIPELINE_TIME_CLS}>{formatTime(execution.created_at)}</span>
                    {execution.completed_at && (
                      <span className={PIPELINE_STEP_TIMING_CLS}>
                        {formatDuration(execution.created_at, execution.completed_at)}
                      </span>
                    )}
                    <ChevronIcon expanded={expanded.has(execution.id)} />
                  </div>
                </div>

                {expanded.has(execution.id) && (
                  <div className={EXEC_DETAILS_CLS}>
                    {/* Trace link */}
                    {execution.trace_id && onNavigateToTrace && (
                      <div className="mb-4">
                        <button
                          type="button"
                          className={PIPELINE_BTN_CLS}
                          onClick={() => onNavigateToTrace(execution.trace_id!)}
                          title="View telemetry trace for this execution"
                        >
                          <TraceIcon />
                          View Trace
                        </button>
                      </div>
                    )}

                    {/* Approval banner */}
                    {execution.status === 'waiting_approval' && (() => {
                      const waitingStep = execution.steps.find(
                        (s) => s.status === 'waiting_approval' && s.approval_token
                      )
                      return waitingStep?.approval_token ? (
                        <div className={PIPELINE_APPROVAL_CLS}>
                          <div className={PIPELINE_APPROVAL_MESSAGE_CLS}>
                            <AlertIcon />
                            <span>Step "{waitingStep.step_id}" requires approval</span>
                          </div>
                          <div className={PIPELINE_APPROVAL_ACTIONS_CLS}>
                            <button
                              type="button"
                              className={`${PIPELINE_BTN_CLS} ${PIPELINE_BTN_APPROVE_CLS}`}
                              onClick={() => handleApprove(waitingStep.approval_token!)}
                              disabled={actionLoading === waitingStep.approval_token}
                            >
                              {actionLoading === waitingStep.approval_token ? 'Approving...' : 'Approve'}
                            </button>
                            <button
                              type="button"
                              className={`${PIPELINE_BTN_CLS} ${PIPELINE_BTN_REJECT_CLS}`}
                              onClick={() => handleReject(waitingStep.approval_token!)}
                              disabled={actionLoading === waitingStep.approval_token}
                            >
                              {actionLoading === waitingStep.approval_token ? 'Rejecting...' : 'Reject'}
                            </button>
                          </div>
                        </div>
                      ) : null
                    })()}

                    {/* Steps */}
                    {execution.steps.length > 0 && (
                      <div className={PIPELINE_STEPS_CLS}>
                        {execution.steps.map((step, index) => (
                          <StepDisplay key={step.id} step={step} index={index} />
                        ))}
                      </div>
                    )}

                    {/* Error */}
                    {execution.outputs_json && (() => {
                      try {
                        const outputs = JSON.parse(execution.outputs_json)
                        if (outputs.error) {
                          return (
                            <div className={PIPELINE_ERROR_CLS}>
                              <span>Error: {outputs.error}</span>
                            </div>
                          )
                        }
                      } catch { /* ignore */ }
                      return null
                    })()}

                    {/* Outputs */}
                    {execution.status === 'completed' && execution.outputs_json && (
                      <div className={OUTPUTS_CLS}>
                        <h4 className={OUTPUTS_HEADING_CLS}>Outputs</h4>
                        <pre className={OUTPUTS_PRE_CLS}>{formatJson(execution.outputs_json)}</pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {showPagination && (
        <PaginationFooter
          total={total!}
          limit={limit!}
          offset={offset!}
          onOffsetChange={onOffsetChange!}
        />
      )}
    </div>
  )
}
