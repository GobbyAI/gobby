import { useState } from "react";
import { ActivityRowStatusDot } from "../../activity/ActivityRowStatusDot";
import { formatDuration, formatJson } from "./executionFormatters";
import { getExecStatusKind } from "../../../lib/pipelineColors";
import { cn } from "../../../lib/utils";
import { Badge, type BadgeProps } from "../../ui/Badge";
import { Button } from "../../ui/Button";
import { Card } from "../../ui/Card";
import { coarseHitAreaCls } from "../../ui/controlStyles";

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

function getBadgeVariant(status: string): BadgeProps["variant"] {
  if (status === "completed" || status === "success") return "success";
  if (status === "failed" || status === "error") return "error";
  if (status === "running") return "info";
  if (
    status === "waiting_approval" ||
    status === "skipped" ||
    status === "timeout" ||
    status === "interrupted"
  ) {
    return "warning";
  }
  return "default";
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <Badge variant={getBadgeVariant(status)} className="uppercase tracking-wider">
      {STATUS_LABELS[status] || status}
    </Badge>
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

// ── Step Display ──
//
// The "card" layout renders each step as a self-contained tinted card with
// padded header/output/error sections. The "timeline" layout renders steps
// stacked vertically against a left rail, with a status-colored dot on each
// row (replaces the former `.pipeline-steps-timeline .pipeline-step::before`
// pseudo-element). PipelinesTab activity-panel detail uses "timeline";
// ReportingTab drilldowns use "card".

function getStepStatusClass(status: string): string {
  if (status === "completed" || status === "success") {
    return "text-[var(--color-success-foreground)]";
  }
  if (status === "failed" || status === "error" || status === "timeout") {
    return "text-[var(--color-error)]";
  }
  if (status === "running") return "text-[var(--color-info)]";
  if (status === "waiting_approval" || status === "skipped") {
    return "text-[var(--color-warning-foreground)]";
  }
  return "text-[var(--text-muted)]";
}

export type StepLayout = "card" | "timeline";

export function StepDisplay({
  step,
  index,
  layout = "card",
}: {
  step: StepData;
  index: number;
  layout?: StepLayout;
}) {
  const [showOutput, setShowOutput] = useState(false);

  const inner = (
    <>
      <Button
        type="button"
        variant="ghost"
        className={cn(
          coarseHitAreaCls,
          "h-auto w-full justify-between rounded-none border-0 px-3 py-2 text-left hover:bg-[var(--bg-secondary)]",
        )}
        aria-expanded={showOutput}
        onClick={() => setShowOutput(!showOutput)}
      >
        <div className="flex items-center gap-2">
          <span className="font-[inherit] text-sm text-[var(--text-muted)]">{index + 1}.</span>
          <span className="text-base">{step.step_id}</span>
        </div>
        <div className="flex items-center gap-2">
          {step.started_at && step.completed_at && (
            <span className="font-[inherit] text-xs text-[var(--text-muted)] tabular-nums">
              {formatDuration(step.started_at, step.completed_at)}
            </span>
          )}
          {layout === "card" && <StepStatusIcon status={step.status} />}
          {step.output_json && <ChevronIcon expanded={showOutput} />}
        </div>
      </Button>

      {showOutput && step.output_json && (
        <div className="border-t border-border bg-[var(--bg-secondary)] px-3 py-2">
          <pre className="m-0 max-h-[200px] overflow-y-auto whitespace-pre-wrap break-words font-mono text-sm leading-[1.5] text-[var(--text-secondary)]">
            {formatJson(step.output_json)}
          </pre>
        </div>
      )}

      {step.error && (
        <div className="border-t border-border bg-[var(--color-error-soft)] px-3 py-2 text-md text-[var(--color-error)] [[data-theme=light]_&]:bg-[color-mix(in_srgb,var(--color-error)_6%,transparent)]">
          <span>{step.error}</span>
        </div>
      )}
    </>
  );

  if (layout === "timeline") {
    return (
      <div className="relative ml-3 pl-3 border-l border-border last:border-l-transparent">
        <span className="absolute left-[-8px] top-2.5 inline-flex size-4 items-center justify-center bg-[var(--bg-primary)]">
          <StepStatusIcon status={step.status} />
        </span>
        {inner}
      </div>
    );
  }

  return (
    <Card className="overflow-hidden rounded-md border-0 bg-[var(--bg-tertiary)]">
      {inner}
    </Card>
  );
}

export function StepStatusIcon({ status }: { status: string }) {
  let icon;

  switch (status) {
    case "completed":
    case "success":
      icon = <CheckIcon />;
      break;
    case "failed":
    case "error":
      icon = <XIcon />;
      break;
    case "running":
      icon = <Spinner />;
      break;
    case "waiting_approval":
      icon = <ClockIcon />;
      break;
    case "skipped":
      icon = <SkipIcon />;
      break;
    case "timeout":
      icon = <AlertIcon />;
      break;
    default:
      icon = <CircleIcon />;
  }

  const label = `Step status: ${status.replace(/_/g, " ")}`;

  return (
    <span
      className={cn("inline-flex items-center justify-center", getStepStatusClass(status))}
      role="img"
      aria-label={label}
      title={label}
    >
      {icon}
    </span>
  );
}

// ── Icons ──

export function PipelineIcon({ className }: { className?: string } = {}) {
  return (
    <svg
      className={className}
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
    <ActivityRowStatusDot
      kind={getExecStatusKind(status)}
      pulse={status === "running"}
      label={status}
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

export function TraceIcon() {
  return (
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
      className="mr-1.5 align-middle"
    >
      <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
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
      className="animate-spin"
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
