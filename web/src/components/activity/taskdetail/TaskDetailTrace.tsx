import type { GobbyTaskDetail } from "../../../hooks/useTasks";
import { isValidGithubRepoSlug } from "../../../lib/githubRepo";
import { MetaKVRow } from "./TaskDetailKV";
import { formatTaskDetailDate } from "./taskDetailFormat";

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
    <details className="activity-task-detail-trace">
      <summary className="activity-task-detail-trace__summary">Trace</summary>
      <div className="activity-task-detail-kv activity-task-detail-trace__body">
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
            <div className="activity-task-detail-pillrow">
              {labels.map((l, i) => (
                <span key={`label-${i}`} className="activity-task-detail-label">
                  {l}
                </span>
              ))}
            </div>
          </MetaKVRow>
        )}
        {showAutomationRow && (
          <MetaKVRow label="Automation">
            <div className="activity-task-detail-pillrow">
              {task.allow_automation && (
                <span
                  className="activity-task-detail-pill"
                  title="Dispatcher is allowed to drive this task"
                >
                  Dispatch on
                </span>
              )}
              {isolation && (
                <span
                  className="activity-task-detail-pill activity-task-detail-pill--mono"
                  title="Isolation kind for automated work"
                >
                  {isolation}
                </span>
              )}
              {task.yolo && (
                <span
                  className="activity-task-detail-pill activity-task-detail-pill--warn"
                  title="Dispatcher uses fallback choices instead of escalating"
                >
                  YOLO
                </span>
              )}
              {dispatchFailures > 0 && (
                <span
                  className="activity-task-detail-pill activity-task-detail-pill--blocked"
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
            <div className="activity-task-detail-pillrow">
              {commits.slice(0, 3).map((sha) => (
                <span
                  key={sha}
                  className="activity-task-detail-pill activity-task-detail-pill--mono"
                  title={sha}
                >
                  {sha.slice(0, 7)}
                </span>
              ))}
              {commits.length > 3 && (
                <span className="activity-task-detail-pill activity-task-detail-pill--mono activity-task-detail-pill--more">
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
