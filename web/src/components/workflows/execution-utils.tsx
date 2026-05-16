import { useState } from "react";
import { ActivityRowStatusDot } from "../activity/ActivityRowStatusDot";
import { formatDuration, formatJson } from "./executionFormatters";
import { getExecStatusKind } from "../../lib/pipelineColors";
export { TraceIcon } from "./ReportsPage.icons";

// ── Shared pipeline class constants ──
//
// Used across PipelineExecutionsView and ReportingTab; defined here so
// both consumers can import the same string. Light-theme-specific overrides
// for these classes that involve color-mix() math (rather than swapping a
// theme-aware token) live in `web/src/styles/index.css` under the
// [data-theme="light"] block; everything theme-aware via tokens (badges,
// soft tints) needs no per-theme variant.

export const PIPELINE_BTN_CLS =
  "px-3 py-1.5 rounded-md text-[length:calc(var(--font-size-base)*0.8)] font-medium cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed pointer-coarse:min-h-11";
export const PIPELINE_BTN_APPROVE_CLS =
  "bg-[var(--color-success-soft)] border border-[var(--color-success-foreground)] text-[var(--color-success-foreground)] hover:not-disabled:bg-[color-mix(in_srgb,var(--color-success-foreground)_20%,transparent)]";
export const PIPELINE_BTN_REJECT_CLS =
  "bg-[var(--color-error-soft)] border border-[var(--color-error)] text-[var(--color-error)] hover:not-disabled:bg-[color-mix(in_srgb,var(--color-error)_20%,transparent)]";

export const PIPELINE_APPROVAL_CLS =
  "bg-[var(--color-warning-soft)] border border-[var(--color-warning-soft)] [[data-theme=light]_&]:bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] [[data-theme=light]_&]:border-[var(--color-warning-foreground)] rounded-md p-3 mb-3";
export const PIPELINE_APPROVAL_MESSAGE_CLS =
  "flex items-center gap-2 mb-3 text-[var(--color-warning-foreground)] [&>svg]:shrink-0";
export const PIPELINE_APPROVAL_ACTIONS_CLS = "flex gap-2";

export const PIPELINE_ERROR_CLS =
  "bg-[var(--color-error-soft)] border border-[var(--color-error)] [[data-theme=light]_&]:bg-[color-mix(in_srgb,var(--color-error)_6%,transparent)] rounded-md p-3 mt-3 text-[var(--color-error)] text-[length:calc(var(--font-size-base)*0.85)]";

export const PIPELINE_STEPS_CLS = "flex flex-col gap-1";

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

const BADGE_BASE_CLS =
  "text-[length:calc(var(--font-size-base)*0.7)] px-2 py-0.5 rounded-full font-medium uppercase tracking-wider";

const BADGE_STATUS_CLS: Record<string, string> = {
  pending: "bg-[var(--bg-tertiary)] text-[var(--text-muted)]",
  running: "bg-[var(--color-info-soft)] text-[var(--color-info)]",
  completed:
    "bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]",
  failed: "bg-[var(--color-error-soft)] text-[var(--color-error)]",
  waiting_approval:
    "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]",
  skipped: "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]",
  success:
    "bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]",
  error: "bg-[var(--color-error-soft)] text-[var(--color-error)]",
  timeout:
    "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]",
  interrupted:
    "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]",
  cancelled: "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
  provider: "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
  model: "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
  mode: "bg-[var(--bg-tertiary)] text-[var(--text-secondary)]",
};

export function StatusBadge({ status }: { status: string }) {
  const variant = BADGE_STATUS_CLS[status] ?? BADGE_STATUS_CLS.pending;
  return (
    <span className={`${BADGE_BASE_CLS} ${variant}`}>
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

// ── Step Display ──
//
// The "card" layout renders each step as a self-contained tinted card with
// padded header/output/error sections. The "timeline" layout renders steps
// stacked vertically against a left rail, with a status-colored dot on each
// row (replaces the former `.pipeline-steps-timeline .pipeline-step::before`
// pseudo-element). PipelinesTab activity-panel detail uses "timeline";
// PipelineExecutionsView and ReportingTab drilldowns use "card".

const STEP_CARD_WRAPPER_CLS =
  "bg-[var(--bg-tertiary)] rounded-md overflow-hidden";
const STEP_HEADER_CLS =
  "flex items-center justify-between px-3 py-2 cursor-pointer hover:bg-[var(--bg-secondary)] pointer-coarse:min-h-11";
const STEP_INFO_CLS = "flex items-center gap-2";
const STEP_INDEX_CLS =
  "text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)] font-[inherit]";
const STEP_NAME_CLS = "text-[length:calc(var(--font-size-base)*0.85)]";
const STEP_META_CLS = "flex items-center gap-2";
const STEP_TIMING_CLS =
  "text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] tabular-nums font-[inherit]";
const STEP_OUTPUT_CLS =
  "px-3 py-2 border-t border-border bg-[var(--bg-secondary)]";
const STEP_OUTPUT_PRE_CLS =
  "font-mono text-[length:calc(var(--font-size-base)*0.75)] whitespace-pre-wrap break-words text-[var(--text-secondary)] m-0 max-h-[200px] overflow-y-auto leading-[1.5]";
const STEP_ERROR_CLS =
  "px-3 py-2 border-t border-border bg-[var(--color-error-soft)] [[data-theme=light]_&]:bg-[color-mix(in_srgb,var(--color-error)_6%,transparent)] text-[var(--color-error)] text-[length:calc(var(--font-size-base)*0.8)]";

const STEP_DOT_BASE_CLS =
  "absolute left-[-5px] top-3 w-2 h-2 rounded-full";

const STEP_DOT_STATUS_CLS: Record<string, string> = {
  completed: "bg-[var(--color-success-foreground)]",
  success: "bg-[var(--color-success-foreground)]",
  failed: "bg-[var(--color-error)]",
  error: "bg-[var(--color-error)]",
  running: "bg-[var(--color-info)] animate-pulse",
  waiting_approval: "bg-[var(--color-warning-foreground)]",
  skipped: "bg-[var(--color-warning-foreground)]",
};

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
      <div
        className={STEP_HEADER_CLS}
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
        <div className={STEP_INFO_CLS}>
          <span className={STEP_INDEX_CLS}>{index + 1}.</span>
          <span className={STEP_NAME_CLS}>{step.step_id}</span>
        </div>
        <div className={STEP_META_CLS}>
          {step.started_at && step.completed_at && (
            <span className={STEP_TIMING_CLS}>
              {formatDuration(step.started_at, step.completed_at)}
            </span>
          )}
          {step.status === "running" && <Spinner />}
          {step.output_json && <ChevronIcon expanded={showOutput} />}
        </div>
      </div>

      {showOutput && step.output_json && (
        <div className={STEP_OUTPUT_CLS}>
          <pre className={STEP_OUTPUT_PRE_CLS}>
            {formatJson(step.output_json)}
          </pre>
        </div>
      )}

      {step.error && (
        <div className={STEP_ERROR_CLS}>
          <span>{step.error}</span>
        </div>
      )}
    </>
  );

  if (layout === "timeline") {
    const dotVariant =
      STEP_DOT_STATUS_CLS[step.status] ?? "bg-[var(--border)]";
    return (
      <div className="relative ml-3 pl-3 border-l border-border last:border-l-transparent">
        <span className={`${STEP_DOT_BASE_CLS} ${dotVariant}`} aria-hidden />
        {inner}
      </div>
    );
  }

  return <div className={STEP_CARD_WRAPPER_CLS}>{inner}</div>;
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
