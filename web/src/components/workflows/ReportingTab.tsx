import { useState, useMemo, useCallback } from "react";
import { usePipelineExecutions } from "../../hooks/usePipelineExecutions";
import type { PipelineExecutionRecord } from "../../hooks/usePipelineExecutions";
import { useAgentRuns } from "../../hooks/useAgentRuns";
import type { AgentRunRecord } from "../../hooks/useAgentRuns";
import {
  StepDisplay,
  ChevronIcon,
  AlertIcon,
  PipelineIcon,
  AgentIcon,
  PipelineStatusDot as StatusDot,
  PIPELINE_BTN_CLS,
  PIPELINE_BTN_APPROVE_CLS,
  PIPELINE_BTN_REJECT_CLS,
  PIPELINE_APPROVAL_CLS,
  PIPELINE_APPROVAL_MESSAGE_CLS,
  PIPELINE_APPROVAL_ACTIONS_CLS,
  PIPELINE_ERROR_CLS,
  PIPELINE_STEPS_CLS,
} from "./execution-utils";
import { formatTime, formatDuration, formatJson } from "./executionFormatters";
import { CronIcon } from "./ReportsPage.icons";
import { WORKFLOWS_CONTENT_CLS } from "./workflows-styles";
import "./pipelines-reporting.css";

const REPORTING_TRIGGER_CLS =
  "inline-flex items-center gap-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_6%,transparent)] border border-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] rounded px-2 py-1 [&>svg]:w-3 [&>svg]:h-3 [&>svg]:text-[var(--color-warning-foreground)]";

const REPORTING_EMPTY_CLS =
  "flex flex-col items-center justify-center px-4 py-12 text-[var(--text-muted)] gap-3 [&>svg]:opacity-30 [&>svg]:w-8 [&>svg]:h-8 [&>p]:text-[length:calc(var(--font-size-base)*0.85)] [&>p]:m-0";

const REPORTING_FILTER_BAR_CLS =
  "flex items-center justify-between gap-3 py-2 border-b border-border mb-2 flex-wrap";
const REPORTING_FILTER_CHIPS_CLS = "flex items-center gap-1.5 flex-wrap";

type TypeFilter = "all" | "pipelines" | "agents";
type StatusFilter = "all" | "running" | "waiting" | "completed" | "failed";

interface ReportingTabProps {
  searchText: string;
  projectId?: string;
  refreshKey?: number;
}

interface TimelineEntry {
  kind: "pipeline" | "agent";
  id: string;
  name: string;
  status: string;
  created_at: string;
  completed_at: string | null;
  pipeline?: PipelineExecutionRecord;
  agent?: AgentRunRecord;
}

function statusMatchesFilter(status: string, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "running") return status === "running" || status === "pending";
  if (filter === "waiting") return status === "waiting_approval";
  if (filter === "completed")
    return status === "completed" || status === "success";
  if (filter === "failed")
    return (
      status === "failed" ||
      status === "error" ||
      status === "timeout" ||
      status === "cancelled" ||
      status === "interrupted"
    );
  return true;
}

const TYPE_OPTIONS: { value: TypeFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pipelines", label: "Pipelines" },
  { value: "agents", label: "Agents" },
];

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "waiting", label: "Waiting" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

export function ReportingTab({
  searchText,
  projectId,
  refreshKey: _refreshKey,
}: ReportingTabProps) {
  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const {
    executions: pipelineExecutions,
    isLoading: pipelinesLoading,
    approvePipeline,
    rejectPipeline,
  } = usePipelineExecutions(projectId);

  const {
    runs: agentRuns,
    isLoading: agentsLoading,
    cancelRun,
  } = useAgentRuns(projectId);

  // Compute counts for stat chips
  const counts = useMemo(() => {
    const all = [
      ...pipelineExecutions.map((pe) => pe.status),
      ...agentRuns.map((ar) => ar.status),
    ];
    return {
      all: all.length,
      running: all.filter((s) => s === "running" || s === "pending").length,
      waiting: all.filter((s) => s === "waiting_approval").length,
      completed: all.filter((s) => s === "completed" || s === "success").length,
      failed: all.filter(
        (s) =>
          s === "failed" ||
          s === "error" ||
          s === "timeout" ||
          s === "cancelled" ||
          s === "interrupted",
      ).length,
    };
  }, [pipelineExecutions, agentRuns]);

  const timeline = useMemo<TimelineEntry[]>(() => {
    const entries: TimelineEntry[] = [];

    if (typeFilter !== "agents") {
      for (const pe of pipelineExecutions) {
        if (!statusMatchesFilter(pe.status, statusFilter)) continue;
        entries.push({
          kind: "pipeline",
          id: pe.id,
          name: pe.pipeline_name,
          status: pe.status,
          created_at: pe.created_at,
          completed_at: pe.completed_at,
          pipeline: pe,
        });
      }
    }

    if (typeFilter !== "pipelines") {
      for (const ar of agentRuns) {
        if (!statusMatchesFilter(ar.status, statusFilter)) continue;
        entries.push({
          kind: "agent",
          id: ar.id,
          name: ar.workflow_name || "Agent Run",
          status: ar.status,
          created_at: ar.created_at,
          completed_at: ar.completed_at,
          agent: ar,
        });
      }
    }

    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      return entries
        .filter(
          (e) =>
            e.name.toLowerCase().includes(q) || e.id.toLowerCase().includes(q),
        )
        .sort((a, b) => b.created_at.localeCompare(a.created_at));
    }

    return entries.sort((a, b) => b.created_at.localeCompare(a.created_at));
  }, [pipelineExecutions, agentRuns, typeFilter, statusFilter, searchText]);

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }, []);

  const handleApprove = async (token: string) => {
    setActionLoading(token);
    try {
      await approvePipeline(token);
    } catch (e) {
      console.error("Approve failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async (token: string) => {
    setActionLoading(token);
    try {
      await rejectPipeline(token);
    } catch (e) {
      console.error("Reject failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const handleCancel = async (runId: string) => {
    setActionLoading(runId);
    try {
      await cancelRun(runId);
    } catch (e) {
      console.error("Cancel failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const isLoading = pipelinesLoading || agentsLoading;

  return (
    <div className={WORKFLOWS_CONTENT_CLS}>
      {/* Filter bar — matches tasks-filter-bar pattern */}
      <div className={REPORTING_FILTER_BAR_CLS}>
        <div className={REPORTING_FILTER_CHIPS_CLS}>
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={`reporting-stat-chip ${statusFilter === opt.value ? "active" : ""}`}
              onClick={() => setStatusFilter(opt.value)}
            >
              {opt.label}
              <span className="reporting-stat-chip-count">
                {counts[opt.value]}
              </span>
            </button>
          ))}
        </div>
        <div className="reporting-type-tabs">
          {TYPE_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              className={`reporting-type-tab ${typeFilter === opt.value ? "active" : ""}`}
              onClick={() => setTypeFilter(opt.value)}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className="tasks-loading">Loading...</div>
      ) : timeline.length === 0 ? (
        <div className={REPORTING_EMPTY_CLS}>
          <PipelineIcon />
          <p>No executions match the current filters.</p>
        </div>
      ) : (
        <div className="reporting-table-container">
          {/* Column headers */}
          <div className="reporting-table-header">
            <span
              className="reporting-th reporting-th--status"
              style={{ width: 28 }}
            ></span>
            <span
              className="reporting-th reporting-th--kind"
              style={{ width: 28 }}
            ></span>
            <span className="reporting-th reporting-th--name">Name</span>
            <span className="reporting-th reporting-th--id">ID</span>
            <span className="reporting-th reporting-th--time">Time</span>
            <span className="reporting-th reporting-th--duration">
              Duration
            </span>
            <span
              className="reporting-th reporting-th--chevron"
              style={{ width: 24 }}
            ></span>
          </div>

          {/* Rows */}
          {timeline.map((entry) => (
            <div
              key={entry.id}
              className={`reporting-entry reporting-entry--${entry.status}`}
            >
              <div
                className="reporting-row"
                role="button"
                tabIndex={0}
                onClick={() => toggleExpanded(entry.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggleExpanded(entry.id);
                  }
                }}
              >
                <span
                  className="reporting-cell reporting-cell--status"
                  style={{ width: 28 }}
                >
                  <StatusDot status={entry.status} />
                </span>
                <span
                  className="reporting-cell reporting-cell--kind"
                  style={{ width: 28 }}
                >
                  {entry.kind === "pipeline" ? (
                    <svg
                      width="14"
                      height="14"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      aria-hidden="true"
                    >
                      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
                    </svg>
                  ) : (
                    <AgentIcon />
                  )}
                </span>
                <span className="reporting-cell reporting-cell--name">
                  {entry.name}
                </span>
                <span className="reporting-cell reporting-cell--id">
                  {entry.id.slice(0, 12)}
                </span>
                <span className="reporting-cell reporting-cell--time">
                  {formatTime(entry.created_at)}
                </span>
                <span className="reporting-cell reporting-cell--duration">
                  {entry.completed_at
                    ? formatDuration(entry.created_at, entry.completed_at)
                    : entry.status === "running"
                      ? "..."
                      : "—"}
                </span>
                <span
                  className="reporting-cell reporting-cell--chevron"
                  style={{ width: 24 }}
                >
                  <ChevronIcon expanded={expanded.has(entry.id)} />
                </span>
              </div>

              {expanded.has(entry.id) &&
                entry.kind === "pipeline" &&
                entry.pipeline && (
                  <PipelineDrillDown
                    execution={entry.pipeline}
                    actionLoading={actionLoading}
                    onApprove={handleApprove}
                    onReject={handleReject}
                  />
                )}

              {expanded.has(entry.id) &&
                entry.kind === "agent" &&
                entry.agent && (
                  <AgentDrillDown
                    run={entry.agent}
                    actionLoading={actionLoading}
                    onCancel={handleCancel}
                  />
                )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


// ── Pipeline Drill-Down ──

function PipelineDrillDown({
  execution,
  actionLoading,
  onApprove,
  onReject,
}: {
  execution: PipelineExecutionRecord;
  actionLoading: string | null;
  onApprove: (token: string) => Promise<void>;
  onReject: (token: string) => Promise<void>;
}) {
  const [showConfig, setShowConfig] = useState(false);

  return (
    <div className="reporting-drilldown">
      {/* Trigger info */}
      {execution.cron_job_name && (
        <div className="reporting-section">
          <span className={REPORTING_TRIGGER_CLS}>
            <CronIcon />
            {execution.cron_job_name} &middot; {execution.cron_expr}
          </span>
        </div>
      )}

      {/* Definition snapshot */}
      {execution.definition_json && (
        <div className="reporting-section">
          <button
            type="button"
            className="reporting-toggle"
            onClick={() => setShowConfig(!showConfig)}
          >
            <ChevronIcon expanded={showConfig} />
            Pipeline Config
          </button>
          {showConfig && (
            <div className="reporting-code-block">
              <pre>{formatJson(execution.definition_json)}</pre>
            </div>
          )}
        </div>
      )}

      {/* Inputs */}
      {execution.inputs_json && (
        <div className="reporting-section">
          <span className="reporting-section-label">Inputs</span>
          <div className="reporting-code-block">
            <pre>{formatJson(execution.inputs_json)}</pre>
          </div>
        </div>
      )}

      {/* Parent link */}
      {execution.parent_execution_id && (
        <div className="reporting-section">
          <div className="reporting-meta-row">
            <span className="reporting-meta-key">Parent</span>
            <span className="reporting-meta-value">
              {execution.parent_execution_id.slice(0, 12)}
            </span>
          </div>
        </div>
      )}

      {/* Approval banner */}
      {execution.status === "waiting_approval" &&
        (() => {
          const waitingStep = execution.steps.find(
            (s) => s.status === "waiting_approval" && s.approval_token,
          );
          return waitingStep?.approval_token ? (
            <div className={PIPELINE_APPROVAL_CLS}>
              <div className={PIPELINE_APPROVAL_MESSAGE_CLS}>
                <AlertIcon />
                <span>
                  Step &ldquo;{waitingStep.step_id}&rdquo; requires approval
                </span>
              </div>
              <div className={PIPELINE_APPROVAL_ACTIONS_CLS}>
                <button
                  type="button"
                  className={`${PIPELINE_BTN_CLS} ${PIPELINE_BTN_APPROVE_CLS}`}
                  onClick={() => onApprove(waitingStep.approval_token!)}
                  disabled={actionLoading === waitingStep.approval_token}
                >
                  {actionLoading === waitingStep.approval_token
                    ? "Approving..."
                    : "Approve"}
                </button>
                <button
                  type="button"
                  className={`${PIPELINE_BTN_CLS} ${PIPELINE_BTN_REJECT_CLS}`}
                  onClick={() => onReject(waitingStep.approval_token!)}
                  disabled={actionLoading === waitingStep.approval_token}
                >
                  {actionLoading === waitingStep.approval_token
                    ? "Rejecting..."
                    : "Reject"}
                </button>
              </div>
            </div>
          ) : null;
        })()}

      {/* Steps */}
      {execution.steps.length > 0 && (
        <div className="reporting-section">
          <span className="reporting-section-label">Steps</span>
          <div className={PIPELINE_STEPS_CLS}>
            {execution.steps.map((step, index) => (
              <StepDisplay key={step.id} step={step} index={index} />
            ))}
          </div>
        </div>
      )}

      {/* Error */}
      {execution.outputs_json &&
        (() => {
          try {
            const outputs = JSON.parse(execution.outputs_json);
            if (outputs.error) {
              return (
                <div className={PIPELINE_ERROR_CLS}>
                  <span>Error: {outputs.error}</span>
                </div>
              );
            }
          } catch (e) {
            console.warn("Failed to parse outputs_json:", e);
          }
          return null;
        })()}

      {/* Outputs */}
      {execution.status === "completed" && execution.outputs_json && (
        <div className="reporting-section">
          <span className="reporting-section-label">Outputs</span>
          <div className="reporting-code-block">
            <pre>{formatJson(execution.outputs_json)}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Agent Drill-Down ──

function AgentDrillDown({
  run,
  actionLoading,
  onCancel,
}: {
  run: AgentRunRecord;
  actionLoading: string | null;
  onCancel: (runId: string) => Promise<void>;
}) {
  const [showPrompt, setShowPrompt] = useState(false);
  const [showResult, setShowResult] = useState(false);

  const totalTokens =
    (run.usage_input_tokens || 0) + (run.usage_output_tokens || 0);

  return (
    <div className="reporting-drilldown">
      {/* Header badges */}
      <div className="reporting-badges-row">
        <span className="reporting-tag reporting-tag--provider">
          {run.provider}
        </span>
        {run.model && (
          <span className="reporting-tag reporting-tag--model">
            {run.model}
          </span>
        )}
        <span className="reporting-tag reporting-tag--mode">{run.mode}</span>
      </div>

      {/* Prompt */}
      <div className="reporting-section">
        <button
          type="button"
          className="reporting-toggle"
          onClick={() => setShowPrompt(!showPrompt)}
        >
          <ChevronIcon expanded={showPrompt} />
          Prompt
        </button>
        {showPrompt && (
          <div className="reporting-code-block">
            <pre>{run.prompt}</pre>
          </div>
        )}
      </div>

      {/* Stats grid */}
      <div className="reporting-section">
        <span className="reporting-section-label">Stats</span>
        <div className="reporting-stats-grid">
          <div className="reporting-stat">
            <span className="reporting-stat-label">Turns</span>
            <span className="reporting-stat-value">{run.turns_used}</span>
          </div>
          <div className="reporting-stat">
            <span className="reporting-stat-label">Tool Calls</span>
            <span className="reporting-stat-value">{run.tool_calls_count ?? 0}</span>
          </div>
          {run.started_at && run.completed_at && (
            <div className="reporting-stat">
              <span className="reporting-stat-label">Duration</span>
              <span className="reporting-stat-value">
                {formatDuration(run.started_at, run.completed_at)}
              </span>
            </div>
          )}
          {totalTokens > 0 && (
            <>
              <div className="reporting-stat">
                <span className="reporting-stat-label">Input Tokens</span>
                <span className="reporting-stat-value">
                  {(run.usage_input_tokens || 0).toLocaleString()}
                </span>
              </div>
              <div className="reporting-stat">
                <span className="reporting-stat-label">Output Tokens</span>
                <span className="reporting-stat-value">
                  {(run.usage_output_tokens || 0).toLocaleString()}
                </span>
              </div>
            </>
          )}
          {(run.usage_cache_read_tokens || 0) > 0 && (
            <div className="reporting-stat">
              <span className="reporting-stat-label">Cache Hit</span>
              <span className="reporting-stat-value">
                {(run.usage_cache_read_tokens || 0).toLocaleString()}
              </span>
            </div>
          )}
        </div>
      </div>

      {/* Summary markdown */}
      {run.summary_markdown && (
        <div className="reporting-section">
          <span className="reporting-section-label">Summary</span>
          <div className="reporting-code-block">
            <pre>{run.summary_markdown}</pre>
          </div>
        </div>
      )}

      {/* Metadata rows */}
      {(run.task_id || run.worktree_id || run.clone_id || run.git_branch) && (
        <div className="reporting-section">
          {run.task_id && (
            <div className="reporting-meta-row">
              <span className="reporting-meta-key">Task</span>
              <span className="reporting-meta-value">{run.task_id}</span>
            </div>
          )}
          {(run.worktree_id || run.clone_id) && (
            <div className="reporting-meta-row">
              <span className="reporting-meta-key">Isolation</span>
              <span className="reporting-meta-value">
                {run.worktree_id
                  ? `worktree: ${run.worktree_id}`
                  : `clone: ${run.clone_id}`}
              </span>
            </div>
          )}
          {run.git_branch && (
            <div className="reporting-meta-row">
              <span className="reporting-meta-key">Branch</span>
              <span className="reporting-meta-value">{run.git_branch}</span>
            </div>
          )}
        </div>
      )}

      {/* Result (success) */}
      {run.status === "success" && run.result && (
        <div className="reporting-section">
          <button
            type="button"
            className="reporting-toggle"
            onClick={() => setShowResult(!showResult)}
          >
            <ChevronIcon expanded={showResult} />
            Result
          </button>
          {showResult && (
            <div className="reporting-code-block">
              <pre>{run.result}</pre>
            </div>
          )}
        </div>
      )}

      {/* Error */}
      {(run.status === "error" || run.status === "timeout") && run.error && (
        <div className={PIPELINE_ERROR_CLS}>
          <span>Error: {run.error}</span>
        </div>
      )}

      {/* Cancel button */}
      {run.status === "running" && (
        <div className="reporting-section">
          <button
            type="button"
            className={`${PIPELINE_BTN_CLS} ${PIPELINE_BTN_REJECT_CLS}`}
            onClick={() => onCancel(run.id)}
            disabled={actionLoading === run.id}
          >
            {actionLoading === run.id ? "Cancelling..." : "Cancel Agent"}
          </button>
        </div>
      )}
    </div>
  );
}
