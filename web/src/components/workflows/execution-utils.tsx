import { useState } from "react";
import { formatDuration, formatJson } from "./executionFormatters";
import { getExecStatusColorVar } from "../../lib/pipelineColors";
export { TraceIcon } from "./ReportsPage.icons";

// ── Status Badge ──

const STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  waiting_approval: "Waiting",
  cancelled: "Cancelled",
  interrupted: "Interrupted",
  skipped: "Skipped",
  success: "Success",
  error: "Error",
  timeout: "Timeout",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`pipeline-badge pipeline-badge--${status}`}>
      {STATUS_LABELS[status] || status}
    </span>
  );
}

// ── Step Display ──

export interface StepData {
  id: number;
  step_id: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  output_json: string | null;
  error: string | null;
  approval_token?: string | null;
}

export function StepDisplay({
  step,
  index,
}: {
  step: StepData;
  index: number;
}) {
  const [showOutput, setShowOutput] = useState(false);

  return (
    <div className={`pipeline-step pipeline-step--${step.status}`}>
      <div
        className="pipeline-step-header"
        role="button"
        tabIndex={0}
        onClick={() => setShowOutput(!showOutput)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            setShowOutput(!showOutput);
          }
        }}
      >
        <div className="pipeline-step-info">
          <span className="pipeline-step-index">{index + 1}.</span>
          <span className="pipeline-step-name">{step.step_id}</span>
        </div>
        <div className="pipeline-execution-meta">
          {step.started_at && step.completed_at && (
            <span className="pipeline-step-timing">
              {formatDuration(step.started_at, step.completed_at)}
            </span>
          )}
          {step.status === "running" && <Spinner />}
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
  );
}

export function StepStatusIcon({ status }: { status: string }) {
  switch (status) {
    case "completed":
    case "success":
      return <CheckIcon />;
    case "failed":
    case "error":
      return <XIcon />;
    case "running":
      return <CircleIcon className="running" />;
    case "waiting_approval":
      return <ClockIcon />;
    case "skipped":
      return <SkipIcon />;
    case "timeout":
      return <ClockIcon />;
    default:
      return <CircleIcon />;
  }
}

// ── Icons ──

export function PipelineIcon() {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
    </svg>
  );
}

export function PipelineStatusDot({ status }: { status: string }) {
  return (
    <span
      className="pipeline-status-dot"
      style={{ backgroundColor: getExecStatusColorVar(status) }}
      title={status}
    />
  );
}

export function AgentIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="8" r="5" />
      <path d="M20 21a8 8 0 1 0-16 0" />
    </svg>
  );
}

export function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
      style={{
        transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
        transition: "transform 0.2s",
      }}
    >
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

export function AlertIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <line x1="12" y1="8" x2="12" y2="12" />
      <line x1="12" y1="16" x2="12.01" y2="16" />
    </svg>
  );
}

export function CheckIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function XIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

export function CircleIcon({ className }: { className?: string }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
      className={className}
    >
      <circle cx="12" cy="12" r="10" />
    </svg>
  );
}

export function ClockIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

export function SkipIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <polygon points="5 4 15 12 5 20 5 4" />
      <line x1="19" y1="5" x2="19" y2="19" />
    </svg>
  );
}

export function Spinner() {
  return (
    <svg
      className="pipeline-spinner"
      width="14"
      height="14"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <circle
        cx="12"
        cy="12"
        r="10"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeDasharray="31.4"
        strokeDashoffset="10"
      />
    </svg>
  );
}
