import { useState } from 'react'
import type { PipelineExecutionRecord } from '../../hooks/usePipelineExecutions'
import {
  StatusBadge,
  StepDisplay,
  ChevronIcon,
  AlertIcon,
  PipelineIcon,
  TraceIcon,
} from './execution-utils'
import { formatTime, formatDuration, formatJson } from './executionFormatters'
import './PipelinesPage.css'

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
    <div className="pipeline-pagination-footer">
      <span className="pipeline-pagination-ribbon">
        {start}–{end} of {total}
      </span>
      <div className="pipeline-pagination-buttons">
        <button
          type="button"
          className="pipeline-btn"
          disabled={!hasPrev}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
        >
          Prev
        </button>
        <button
          type="button"
          className="pipeline-btn"
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
    <div className="workflows-content">
      {/* Status filter chips */}
      <div className="pipeline-exec-filters">
        {STATUS_FILTERS.map(({ value, label }) => (
          <button
            key={value}
            type="button"
            className={`pipeline-exec-filter-chip ${(filters.status || '') === value ? 'pipeline-exec-filter-chip--active' : ''}`}
            onClick={() => onFiltersChange({ ...filters, status: value || undefined })}
          >
            {label}
          </button>
        ))}
      </div>

      {isLoading ? (
        <div className="workflows-loading">Loading executions...</div>
      ) : executions.length === 0 ? (
        <div className="pipeline-panel pipeline-panel--empty">
          <div className="pipeline-empty">
            <PipelineIcon />
            <p>No pipeline executions{filters.status ? ` with status "${filters.status}"` : ''}</p>
          </div>
        </div>
      ) : (
        <div className="pipeline-panel">
          <div className="pipeline-list" style={{ maxHeight: 'none' }}>
            {executions.map((execution) => (
              <div
                key={execution.id}
                className={`pipeline-execution pipeline-execution--${execution.status}`}
              >
                <div
                  className="pipeline-execution-header"
                  role="button"
                  tabIndex={0}
                  onClick={() => toggleExpanded(execution.id)}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleExpanded(execution.id) } }}
                >
                  <div className="pipeline-execution-info">
                    <StatusBadge status={execution.status} />
                    <span className="pipeline-name">{execution.pipeline_name}</span>
                    <span className="pipeline-id">{execution.id.slice(0, 12)}</span>
                  </div>
                  <div className="pipeline-execution-meta">
                    <span className="pipeline-time">{formatTime(execution.created_at)}</span>
                    {execution.completed_at && (
                      <span className="pipeline-step-timing">
                        {formatDuration(execution.created_at, execution.completed_at)}
                      </span>
                    )}
                    <ChevronIcon expanded={expanded.has(execution.id)} />
                  </div>
                </div>

                {expanded.has(execution.id) && (
                  <div className="pipeline-execution-details">
                    {/* Trace link */}
                    {execution.trace_id && onNavigateToTrace && (
                      <div className="pipeline-trace-link" style={{ marginBottom: '1rem' }}>
                        <button
                          type="button"
                          className="pipeline-btn"
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
                        <div className="pipeline-approval">
                          <div className="pipeline-approval-message">
                            <AlertIcon />
                            <span>Step "{waitingStep.step_id}" requires approval</span>
                          </div>
                          <div className="pipeline-approval-actions">
                            <button
                              type="button"
                              className="pipeline-btn pipeline-btn--approve"
                              onClick={() => handleApprove(waitingStep.approval_token!)}
                              disabled={actionLoading === waitingStep.approval_token}
                            >
                              {actionLoading === waitingStep.approval_token ? 'Approving...' : 'Approve'}
                            </button>
                            <button
                              type="button"
                              className="pipeline-btn pipeline-btn--reject"
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
                      <div className="pipeline-steps">
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
                            <div className="pipeline-error">
                              <span>Error: {outputs.error}</span>
                            </div>
                          )
                        }
                      } catch { /* ignore */ }
                      return null
                    })()}

                    {/* Outputs */}
                    {execution.status === 'completed' && execution.outputs_json && (
                      <div className="pipeline-outputs">
                        <h4>Outputs</h4>
                        <pre>{formatJson(execution.outputs_json)}</pre>
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
