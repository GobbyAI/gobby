import { memo, useState } from "react";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Markdown } from "../chat/Markdown";
import { markdownBodyClassName } from "../shared/MarkdownBody";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { formatDateTime } from "../shared/executions/executionFormatters";
import { useFeedbackReview } from "../../hooks/useFeedbackReview";
import type {
  FeedbackRun,
  FeedbackStatus,
} from "../../hooks/useFeedbackReview";
import { ActivityPanelEmpty, FeedbackEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityRowStatusDot, type StatusKind } from "./ActivityRowStatusDot";

const RUN_STATUS: Record<string, { kind: StatusKind; label: string }> = {
  completed: { kind: "success", label: "Completed" },
  partial: { kind: "warning", label: "Partial" },
  running: { kind: "active", label: "Running" },
  failed: { kind: "error", label: "Failed" },
  interrupted: { kind: "stopped", label: "Interrupted" },
};

function runStatus(status: string): { kind: StatusKind; label: string } {
  return RUN_STATUS[status] ?? { kind: "disabled", label: status };
}

export const FeedbackTab = memo(function FeedbackTab() {
  const { status, runs, isLoading, error } = useFeedbackReview();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(45);
  const selectedRun = runs.find((run) => run.id === selectedRunId) ?? null;

  if (isLoading) {
    return <ActivityPanelEmpty body="Loading feedback review…" />;
  }

  return (
    <div className="flex h-full flex-col">
      {error && (
        <p
          role="alert"
          className="border-b border-border px-3 py-1.5 text-xs text-destructive"
        >
          {error}
        </p>
      )}
      {status && <StatusStrip status={status} />}

      <div
        className={`overflow-y-auto ${selectedRun ? "border-b border-border" : "flex-1"}`}
        style={selectedRun ? { height: `${topHeight}%` } : undefined}
      >
        {runs.length === 0
          ? !error && (
              <ActivityPanelEmpty
                icon={<FeedbackEmptyIcon />}
                heading="Feedback"
                body="Review runs appear here after the nightly feedback review"
              />
            )
          : runs.map((run) => {
              const { kind, label } = runStatus(run.status);
              return (
                <Button
                  key={run.id}
                  type="button"
                  variant="ghost"
                  data-testid="feedback-run-row"
                  aria-pressed={selectedRunId === run.id}
                  className={`flex min-h-[var(--activity-panel-row-height)] w-full cursor-pointer appearance-none items-center justify-between gap-2 rounded-none border-0 border-b border-[var(--border)] bg-transparent px-3 py-2 text-left [color:inherit] transition-colors duration-100 [font:inherit] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11 ${selectedRunId === run.id ? "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] hover:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]" : ""} ${coarseHitAreaCls}`}
                  onClick={() => setSelectedRunId(run.id)}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <ActivityRowStatusDot kind={kind} />
                    <span className="truncate text-sm text-foreground">
                      {label}
                    </span>
                    {run.dry_run && (
                      <span className="shrink-0 text-2xs text-muted-foreground">
                        Dry run
                      </span>
                    )}
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-2xs text-muted-foreground">
                    <span className="tabular-nums">
                      {run.rows_considered} rows
                    </span>
                    <span>{formatDateTime(run.created_at)}</span>
                  </span>
                </Button>
              );
            })}
      </div>

      {selectedRun && (
        <>
          <ResizeHandle
            direction="vertical"
            onResize={setTopHeight}
            panelHeight={topHeight}
            minHeight={15}
            maxHeight={80}
          />
          <RunDetail run={selectedRun} />
        </>
      )}
    </div>
  );
});

function StatusStrip({ status }: { status: FeedbackStatus }) {
  const { schedule } = status;
  let scheduleText = "Review not scheduled";
  if (schedule) {
    scheduleText = `Review ${schedule.cron_expr} (${schedule.timezone})`;
    if (!schedule.enabled) {
      scheduleText += " paused";
    } else if (schedule.next_run_at) {
      scheduleText += `, next ${formatDateTime(schedule.next_run_at)}`;
    }
  }
  return (
    <div
      data-testid="feedback-status-strip"
      className="flex h-10 shrink-0 items-center gap-3 border-b border-border bg-[var(--bg-secondary)] px-3 text-xs"
    >
      <span className="shrink-0 font-medium text-foreground tabular-nums">
        {status.backlog} unreviewed
      </span>
      <span className="truncate text-muted-foreground" title={scheduleText}>
        {scheduleText}
      </span>
    </div>
  );
}

function RunDetail({ run }: { run: FeedbackRun }) {
  const { kind, label } = runStatus(run.status);
  const filed = run.actions?.filed ?? [];
  const deduplicated = run.actions?.deduplicated ?? 0;
  return (
    <div
      data-testid="feedback-run-detail"
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-border bg-[var(--bg-secondary)] px-3 text-xs">
        <ActivityRowStatusDot kind={kind} />
        <span className="font-medium text-foreground">{label}</span>
        {run.dry_run && <span className="text-muted-foreground">Dry run</span>}
        <span className="ml-auto shrink-0 text-muted-foreground tabular-nums">
          {run.rows_considered} rows, {deduplicated} deduplicated
        </span>
      </div>
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {run.error && (
          <p role="alert" className="mb-2 text-xs text-destructive">
            {run.error}
          </p>
        )}
        <div className="mb-3">
          {filed.length === 0 ? (
            <p className="text-xs text-muted-foreground">No tasks filed</p>
          ) : (
            <ul aria-label="Filed tasks" className="m-0 list-none p-0">
              {filed.map((task) => (
                <li
                  key={task.task_ref}
                  className="flex min-w-0 items-baseline gap-2 py-0.5 text-xs"
                >
                  <span className="shrink-0 font-mono text-foreground select-all">
                    {task.task_ref}
                  </span>
                  <span
                    className="truncate text-muted-foreground"
                    title={task.title}
                  >
                    {task.title}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {run.digest_md ? (
          <div
            className={`message-content text-[length:var(--text-base)] leading-[1.6] text-[var(--text-secondary)] ${markdownBodyClassName}`}
          >
            <Markdown
              content={run.digest_md}
              id={`feedback-digest-${run.id}`}
            />
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">No digest recorded</p>
        )}
      </div>
    </div>
  );
}
