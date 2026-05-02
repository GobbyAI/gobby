import { useMemo } from "react";

import { Markdown } from "../chat/Markdown";
import type { DependencyTree, GobbyTask, GobbyTaskDetail } from "../../hooks/useTasks";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  getTaskStateSummary,
  TASK_STATE_LABELS,
  type TaskDisplayState,
} from "../../lib/taskState";
import { TaskStatusStrip } from "../tasks/TaskStatusStrip";

export type { GobbyTaskDetail };

export interface ParentTaskRef {
  id: string;
  ref: string;
  title: string;
}

interface TasksTabDetailPanelProps {
  task: GobbyTaskDetail;
  parentTask?: ParentTaskRef | null;
  onSelectTask?: (id: string) => void;
  dependencies?: DependencyTree | null;
  subtasks?: GobbyTask[];
}

const SUBTASK_STATE_ORDER: TaskDisplayState[] = [
  "ready",
  "in_progress",
  "needs_review",
  "review_approved",
  "blocked",
  "closed",
];

function formatTaskDetailDate(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }

  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }

  return `${parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })} ${parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function TasksTabDetailPanel({
  task,
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
}: TasksTabDetailPanelProps) {
  const taskState = getCanonicalTaskState(task);
  const ownerLabel = task.agent_name ?? taskState.owner_session_id ?? "Unassigned";
  const ownerMono = !task.agent_name && Boolean(taskState.owner_session_id);
  const stateLabel = getTaskStateSummary(task);
  const categoryLabel = task.category ?? task.task_type;
  const labels = task.labels?.filter(Boolean) ?? [];
  const blockerCount = dependencies?.blockers?.length ?? 0;
  const blockingCount = dependencies?.blocking?.length ?? 0;
  const commits = task.commits?.filter(Boolean) ?? [];

  const { subtaskBuckets, subtaskTotal } = useMemo(() => {
    const states: Partial<Record<TaskDisplayState, number>> = {};
    for (const child of subtasks ?? []) {
      const displayState = getTaskDisplayState(child);
      states[displayState] = (states[displayState] ?? 0) + 1;
    }
    return { subtaskBuckets: states, subtaskTotal: subtasks?.length ?? 0 };
  }, [subtasks]);

  const validationStatus = task.validation_status?.trim() || null;
  const validationFailCount = task.validation_fail_count ?? 0;
  const validationFeedback = task.validation_feedback?.trim() || null;
  const showValidationFeedback =
    validationFeedback !== null &&
    (validationStatus === "failed" || validationFailCount > 0);

  const isolation = task.isolation && task.isolation !== "none" ? task.isolation : null;
  const dispatchFailures = task.dispatch_failure_count ?? 0;
  const showAutomationRow =
    Boolean(task.allow_automation) ||
    Boolean(task.yolo) ||
    isolation !== null ||
    dispatchFailures > 0;

  const prUrl =
    task.github_pr_number != null && task.github_repo
      ? `https://github.com/${task.github_repo}/pull/${task.github_pr_number}`
      : null;
  const prLabel =
    task.github_pr_number != null
      ? task.github_repo
        ? `${task.github_repo}#${task.github_pr_number}`
        : `#${task.github_pr_number}`
      : null;

  const isEscalated = Boolean(task.escalated_at);
  const escalationReason = task.escalation_reason?.trim() || null;
  const preEscalationStatus = task.pre_escalation_status?.trim() || null;

  return (
    <div className="activity-task-detail-card">
      <div className="activity-task-detail-meta">
        <TaskDetailMetaRow
          label="Claimed by"
          value={ownerLabel}
          mono={ownerMono}
          title="Agent or session currently holding this task's claim"
        />
        {task.assigned_agent && (
          <TaskDetailMetaRow
            label="Agent"
            value={task.assigned_agent}
            mono
            title="Agent role assigned to drive this task"
          />
        )}
        <TaskDetailMetaRow label="State" value={stateLabel} />
        {taskState.current_stage && (
          <TaskDetailMetaRow
            label="Stage"
            value={taskState.current_stage.display_name}
            title="Current manifest stage"
          />
        )}
        <TaskDetailMetaRow label="Created" value={formatTaskDetailDate(task.created_at)} />
        <TaskDetailMetaRow label="Updated" value={formatTaskDetailDate(task.updated_at)} />
        <TaskDetailMetaRow label="Category" value={categoryLabel} />
        {parentTask && (
          <TaskDetailParentRow
            parent={parentTask}
            onSelect={onSelectTask}
          />
        )}
        {task.path_cache && <TaskDetailMetaRow label="Path" value={task.path_cache} mono />}
        {validationStatus && (
          <TaskDetailValidationRow
            status={validationStatus}
            failCount={validationFailCount}
          />
        )}
        {prUrl && prLabel && (
          <TaskDetailMetaRow
            label="PR"
            value={prLabel}
            mono
            href={prUrl}
            title="Open PR on GitHub"
          />
        )}
        {task.closed_at && (
          <TaskDetailMetaRow
            label="Closed"
            value={formatTaskDetailDate(task.closed_at)}
          />
        )}
        {task.closed_commit_sha && (
          <TaskDetailMetaRow
            label="Closing commit"
            value={task.closed_commit_sha.slice(0, 7)}
            mono
            title={task.closed_commit_sha}
          />
        )}
        {task.closed_reason && (
          <TaskDetailMetaRow label="Close reason" value={task.closed_reason} />
        )}
        {task.closed_in_session_id && (
          <TaskDetailMetaRow
            label="Closed in"
            value={task.closed_in_session_id}
            mono
            title="Session that closed this task"
          />
        )}
      </div>

      {task.stages?.length > 0 && (
        <div className="activity-task-detail-status">
          <TaskStatusStrip task={task} compact />
        </div>
      )}

      {labels.length > 0 && (
        <div className="activity-task-detail-labels">
          {labels.map((label, index) => (
            <span key={`${label}-${index}`} className="activity-task-detail-label">
              {label}
            </span>
          ))}
        </div>
      )}

      {showAutomationRow && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Automation</div>
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
        </div>
      )}

      {isEscalated && (
        <div className="activity-task-detail-section activity-task-detail-section--escalated">
          <div className="activity-task-detail-section-title">
            Escalated
            {task.escalated_at && (
              <span className="activity-task-detail-section-count">
                {formatTaskDetailDate(task.escalated_at)}
              </span>
            )}
          </div>
          {preEscalationStatus && (
            <div className="activity-task-detail-meta-row">
              <span className="activity-task-detail-meta-label">From</span>
              <span className="activity-task-detail-meta-value">{preEscalationStatus}</span>
            </div>
          )}
          {escalationReason && (
            <div className="activity-task-detail-escalation-reason">{escalationReason}</div>
          )}
        </div>
      )}

      {(blockerCount > 0 || blockingCount > 0) && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Dependencies</div>
          <div className="activity-task-detail-pillrow">
            {blockerCount > 0 && (
              <span
                className="activity-task-detail-pill activity-task-detail-pill--blocked"
                title="Tasks this task depends on"
              >
                Blocked by <strong>{blockerCount}</strong>
              </span>
            )}
            {blockingCount > 0 && (
              <span
                className="activity-task-detail-pill"
                title="Tasks waiting on this task"
              >
                Blocks <strong>{blockingCount}</strong>
              </span>
            )}
          </div>
        </div>
      )}

      {subtaskTotal > 0 && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">
            Subtasks <span className="activity-task-detail-section-count">{subtaskTotal}</span>
          </div>
          <div className="activity-task-detail-pillrow">
            {SUBTASK_STATE_ORDER.map((displayState) => {
              const count = subtaskBuckets[displayState] ?? 0;
              if (count === 0) return null;
              return (
                <span
                  key={displayState}
                  className="activity-task-detail-pill"
                  title={`${TASK_STATE_LABELS[displayState]} subtasks`}
                >
                  <strong>{count}</strong> {TASK_STATE_LABELS[displayState].toLowerCase()}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {commits.length > 0 && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">
            Commits
            {commits.length > 3 && (
              <span className="activity-task-detail-section-count">{commits.length}</span>
            )}
          </div>
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
        </div>
      )}

      {task.description && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Description</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown content={task.description} id={`task-desc-${task.id}`} />
          </div>
        </div>
      )}

      {task.validation_criteria && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Validation</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown
              content={task.validation_criteria}
              id={`task-vc-${task.id}`}
            />
          </div>
        </div>
      )}

      {showValidationFeedback && validationFeedback && (
        <div className="activity-task-detail-section activity-task-detail-section--failed">
          <div className="activity-task-detail-section-title">Validation feedback</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown
              content={validationFeedback}
              id={`task-vf-${task.id}`}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function TaskDetailMetaRow({
  label,
  value,
  mono = false,
  title,
  href,
}: {
  label: string;
  value: string;
  mono?: boolean;
  title?: string;
  href?: string;
}) {
  const valueClass = `activity-task-detail-meta-value${
    mono ? " activity-task-detail-meta-value--mono" : ""
  }`;
  return (
    <div className="activity-task-detail-meta-row" title={title}>
      <span className="activity-task-detail-meta-label">{label}</span>
      {href ? (
        <a
          className={`${valueClass} activity-task-detail-meta-value--link`}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
        >
          {value}
        </a>
      ) : (
        <span className={valueClass}>{value}</span>
      )}
    </div>
  );
}

function TaskDetailValidationRow({
  status,
  failCount,
}: {
  status: string;
  failCount: number;
}) {
  const normalized = status.toLowerCase();
  const variant =
    normalized === "passed" || normalized === "approved"
      ? "ok"
      : normalized === "failed" || normalized === "rejected"
        ? "fail"
        : "neutral";
  return (
    <div className="activity-task-detail-meta-row" title="Validation status">
      <span className="activity-task-detail-meta-label">Validation</span>
      <span className="activity-task-detail-meta-value">
        <span
          className={`activity-task-detail-pill activity-task-detail-pill--${variant}`}
        >
          {status}
        </span>
        {failCount > 0 && (
          <span className="activity-task-detail-validation-fails">
            {" "}
            {failCount} {failCount === 1 ? "fail" : "fails"}
          </span>
        )}
      </span>
    </div>
  );
}

function TaskDetailParentRow({
  parent,
  onSelect,
}: {
  parent: ParentTaskRef;
  onSelect?: (id: string) => void;
}) {
  const handleClick = onSelect ? () => onSelect(parent.id) : undefined;
  return (
    <div className="activity-task-detail-meta-row" title="Parent task">
      <span className="activity-task-detail-meta-label">Parent</span>
      <span className="activity-task-detail-meta-value">
        {handleClick ? (
          <button
            type="button"
            className="activity-task-detail-parent-link"
            onClick={handleClick}
          >
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            <span className="activity-task-detail-parent-title">{parent.title}</span>
          </button>
        ) : (
          <>
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            {" "}
            <span className="activity-task-detail-parent-title">{parent.title}</span>
          </>
        )}
      </span>
    </div>
  );
}
