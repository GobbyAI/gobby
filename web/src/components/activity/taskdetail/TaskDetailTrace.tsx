import type { GobbyTaskDetail } from "../../../types/tasks";
import { isValidGithubRepoSlug } from "../../../lib/githubRepo";
import { cn } from "../../../lib/utils";
import { MetaKVRow } from "./TaskDetailKV";
import { formatTaskDetailDate } from "./taskDetailFormat";

const taskDetailPillClassName =
  "inline-flex h-6 items-center gap-[0.3rem] whitespace-nowrap rounded-full border border-border bg-[var(--bg-tertiary)] px-[0.55rem] text-[length:var(--text-2xs)] font-medium tracking-[0.02em] text-[var(--text-secondary)] [&_strong]:font-semibold [&_strong]:text-[var(--text-primary)]";

/**
 * D5 §5 — collapsed-by-default Trace: timestamps, automation, commits, PR,
 * and close provenance. "Path" is intentionally cut. Escalation is handled
 * by the panel and only rendered when the task is escalated.
 */
export function TaskDetailTrace({ task }: { task: GobbyTaskDetail }) {
  const labels = task.labels?.filter(Boolean) ?? [];
  const commits = task.commits?.filter(Boolean) ?? [];
  const isolation =
    task.isolation && task.isolation !== "none" ? task.isolation : null;
  const dispatchFailures = task.dispatch_failure_count ?? 0;
  const showAutomationRow =
    Boolean(task.allow_automation) ||
    Boolean(task.yolo) ||
    isolation !== null ||
    dispatchFailures > 0;

  const githubRepo = isValidGithubRepoSlug(task.github_repo) ? task.github_repo : null;
  const prUrl =
    task.github_pr_number != null && githubRepo
      ? `https://github.com/${githubRepo}/pull/${task.github_pr_number}`
      : null;
  const prLabel =
    task.github_pr_number != null
      ? githubRepo
        ? `${githubRepo}#${task.github_pr_number}`
        : `#${task.github_pr_number}`
      : null;

  const hasTrace =
    labels.length > 0 ||
    showAutomationRow ||
    commits.length > 0 ||
    prLabel !== null ||
    Boolean(task.closed_commit_sha) ||
    Boolean(task.closed_reason) ||
    Boolean(task.closed_in_session_id);
  if (!hasTrace) return null;

  return (
    <details className="group border-b border-border bg-[var(--bg-primary)]">
      <summary className="cursor-pointer list-none px-4 py-[0.7rem] text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] uppercase tracking-[0.08em] text-[var(--text-muted)] before:mr-[0.4rem] before:inline-block before:tracking-normal before:content-['▸'] before:transition-transform before:duration-[120ms] group-open:before:rotate-90 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent [&::-webkit-details-marker]:hidden">
        Trace
      </summary>
      <div className="flex flex-col border-b-0 bg-[var(--bg-primary)] px-4 pb-[0.95rem] pt-0">
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
                  className="inline-flex h-5 items-center whitespace-nowrap rounded-full border border-border bg-[var(--bg-tertiary)] px-2 font-mono text-[length:var(--text-2xs)] font-medium tracking-[0.02em] text-[var(--text-secondary)]"
                >
                  {l}
                </span>
              ))}
            </div>
          </MetaKVRow>
        )}
        {showAutomationRow && (
          <MetaKVRow label="Automation">
            <div className="flex flex-wrap gap-[0.4rem]">
              {task.allow_automation && (
                <span
                  className={taskDetailPillClassName}
                  title="Dispatcher is allowed to drive this task"
                >
                  Dispatch on
                </span>
              )}
              {isolation && (
                <span
                  className={cn(
                    taskDetailPillClassName,
                    "font-mono tracking-normal",
                  )}
                  title="Isolation kind for automated work"
                >
                  {isolation}
                </span>
              )}
              {task.yolo && (
                <span
                  className={cn(
                    taskDetailPillClassName,
                    "border-[color-mix(in_srgb,var(--accent)_40%,transparent)] bg-[color-mix(in_srgb,var(--accent)_12%,transparent)] font-semibold text-accent",
                  )}
                  title="Dispatcher uses fallback choices instead of escalating"
                >
                  YOLO
                </span>
              )}
              {dispatchFailures > 0 && (
                <span
                  className={cn(
                    taskDetailPillClassName,
                    "border-[color-mix(in_srgb,var(--color-warning-foreground)_35%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_10%,transparent)] text-[var(--color-warning-foreground)] [&_strong]:text-[var(--color-warning-foreground)]",
                  )}
                  title="Consecutive dispatcher failures"
                >
                  <strong>{dispatchFailures}</strong> dispatch fails
                </span>
              )}
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
        {prLabel &&
          (prUrl ? (
            <MetaKVRow label="PR" mono link href={prUrl}>
              {prLabel}
            </MetaKVRow>
          ) : (
            <MetaKVRow label="PR" mono>
              {prLabel}
            </MetaKVRow>
          ))}
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
