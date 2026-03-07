import { useState } from 'react'
import type { PipelineExecutionRecord, PipelineStepExecution } from '../../hooks/usePipelineExecutions'
import './PipelinesPage.css'

interface PipelineExecutionsViewProps {
  executions: PipelineExecutionRecord[]
  isLoading: boolean
  filters: { status?: string; pipeline_name?: string }
  onFiltersChange: (filters: { status?: string; pipeline_name?: string }) => void
  onApprove: (token: string) => Promise<unknown>
  onReject: (token: string) => Promise<unknown>
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
}: PipelineExecutionsViewProps) {
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
    </div>
  )
}

function StepDisplay({ step, index }: { step: PipelineStepExecution; index: number }) {
  const [showOutput, setShowOutput] = useState(false)

  return (
    <div className={`pipeline-step pipeline-step--${step.status}`}>
      <div className="pipeline-step-header" role="button" tabIndex={0} onClick={() => setShowOutput(!showOutput)} onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setShowOutput(!showOutput) } }}>
        <div className="pipeline-step-info">
          <StepStatusIcon status={step.status} />
          <span className="pipeline-step-index">{index + 1}.</span>
          <span className="pipeline-step-name">{step.step_id}</span>
        </div>
        <div className="pipeline-execution-meta">
          {step.started_at && step.completed_at && (
            <span className="pipeline-step-timing">
              {formatDuration(step.started_at, step.completed_at)}
            </span>
          )}
          {step.status === 'running' && <Spinner />}
          {step.output_json && <ChevronIcon expanded={showOutput} />}
        </div>
      </div>

      {showOutput && step.output_json && (
        <div className="pipeline-step-output">
          <pre>{formatJson(step.output_json)}</pre>
        </div>
      )}

      {step.error && (
        <div className="pipeline-step-error">
          <span>{step.error}</span>
        </div>
      )}
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const labels: Record<string, string> = {
    pending: 'Pending',
    running: 'Running',
    completed: 'Completed',
    failed: 'Failed',
    waiting_approval: 'Waiting',
    cancelled: 'Cancelled',
    interrupted: 'Interrupted',
    skipped: 'Skipped',
  }
  return <span className={`pipeline-badge pipeline-badge--${status}`}>{labels[status] || status}</span>
}

function StepStatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return <CheckIcon />
    case 'failed':
      return <XIcon />
    case 'running':
      return <CircleIcon className="running" />
    case 'waiting_approval':
      return <ClockIcon />
    case 'skipped':
      return <SkipIcon />
    default:
      return <CircleIcon />
  }
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function formatDuration(startIso: string, endIso: string): string {
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime()
  if (isNaN(ms) || ms < 0) return ''
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function formatJson(json: string): string {
  try {
    return JSON.stringify(JSON.parse(json), null, 2)
  } catch {
    return json
  }
}

// Icons
function PipelineIcon() {
  return (
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  )
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      aria-hidden="true"
      style={{ transform: expanded ? 'rotate(180deg)' : 'rotate(0deg)', transition: 'transform 0.2s' }}
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  )
}

function AlertIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  )
}

function XIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function CircleIcon({ className }: { className?: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true" className={className}>
      <circle cx="12" cy="12" r="10" />
    </svg>
  )
}

function ClockIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  )
}

function SkipIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <polygon points="5 4 15 12 5 20 5 4" />
      <line x1="19" y1="5" x2="19" y2="19" />
    </svg>
  )
}

function Spinner() {
  return (
    <svg className="pipeline-spinner" width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="31.4" strokeDashoffset="10" />
    </svg>
  )
}
