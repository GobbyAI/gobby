import type { GobbyTaskDetail } from "../../../types/tasks";

import { cn } from "../../../lib/utils";
import { MetaKVRow } from "./TaskDetailKV";
import { formatTaskDetailDate } from "./taskDetailFormat";

const taskDetailPillClassName =
  "inline-flex h-6 items-center gap-[0.3rem] whitespace-nowrap rounded-full border border-border bg-[var(--bg-tertiary)] px-[0.55rem] text-[length:var(--text-2xs)] font-medium tracking-[0.02em] text-[var(--text-secondary)] [&_strong]:font-semibold [&_strong]:text-[var(--text-primary)]";

/**
 * D5 §5 — collapsed-by-default Trace: timestamps, commits,
 * and close provenance. "Path" is intentionally cut. Escalation is handled
 * by the panel and only rendered when the task is escalated.
 */
export function TaskDetailTrace({ task }: { task: GobbyTaskDetail }) {
  const labels = task.labels?.filter(Boolean) ?? [];
  const commits = task.commits?.filter(Boolean) ?? [];
  const hasTrace =
    labels.length > 0 ||
    commits.length > 0 ||
    Boolean(task.closed_commit_sha) ||
    Boolean(task.closed_reason) ||
    Boolean(task.closed_in_session_id);
  if (!hasTrace) return null;

  return (
    <details className="group border-b border-border bg-[var(--bg-primary)]">
      <summary className="cursor-pointer list-none px-4 py-[0.7rem] text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] tracking-[0.08em] text-[var(--text-muted)] uppercase before:mr-[0.4rem] before:inline-block before:tracking-normal before:transition-transform before:duration-[120ms] before:content-['▸'] group-open:before:rotate-90 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
        Trace
      </summary>
      <div className="flex flex-col border-b-0 bg-[var(--bg-primary)] px-4 pt-0 pb-[0.95rem]">
        <MetaKVRow label="Created">
          {formatTaskDetailDate(task.created_at)}
        </MetaKVRow>
        <MetaKVRow label="Updated">
          {formatTaskDetailDate(task.updated_at)}
        </MetaKVRow>
        {task.closed_at && (
          <MetaKVRow label="Closed">
            {formatTaskDetailDate(task.closed_at)}
          </MetaKVRow>
        )}
        {labels.length > 0 && (
          <MetaKVRow label="Labels">
            <div className="flex flex-wrap gap-[0.4rem]">
              {labels.map((l, i) => (
                <span
                  key={`label-${i}`}
                  className="inline-flex h-5 items-center rounded-full border border-border bg-[var(--bg-tertiary)] px-2 font-mono text-[length:var(--text-2xs)] font-medium tracking-[0.02em] whitespace-nowrap text-[var(--text-secondary)]"
                >
                  {l}
                </span>
              ))}
            </div>
          </MetaKVRow>
        )}
        {commits.length > 0 && (
          <MetaKVRow label={`Commits (${commits.length})`}>
            <div className="flex flex-wrap gap-[0.4rem]">
              {commits.slice(0, 3).map((sha) => (
                <span
                  key={sha}
                  className={cn(
                    taskDetailPillClassName,
                    "font-mono tracking-normal",
                  )}
                  title={sha}
                >
                  {sha.slice(0, 7)}
                </span>
              ))}
              {commits.length > 3 && (
                <span
                  className={cn(
                    taskDetailPillClassName,
                    "font-mono tracking-normal text-[var(--text-muted)]",
                  )}
                >
                  +{commits.length - 3}
                </span>
              )}
            </div>
          </MetaKVRow>
        )}
        {task.closed_commit_sha && (
          <MetaKVRow label="Closing commit" mono title={task.closed_commit_sha}>
            {task.closed_commit_sha.slice(0, 7)}
          </MetaKVRow>
        )}
        {task.closed_reason && (
          <MetaKVRow label="Close reason">{task.closed_reason}</MetaKVRow>
        )}
        {task.closed_in_session_id && (
          <MetaKVRow
            label="Closed in"
            mono
            title="Session that closed this task"
          >
            {task.closed_in_session_id}
          </MetaKVRow>
        )}
      </div>
    </details>
  );
}
