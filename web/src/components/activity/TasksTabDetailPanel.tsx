import { useMemo, type ReactNode } from "react";

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
import { relativeTime } from "../../utils/formatTime";

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

type HeroStageVariant =
  | "active"
  | "blocked"
  | "closed"
  | "escalated"
  | "default";

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

function heroStageVariant(
  isEscalated: boolean,
  displayState: TaskDisplayState,
): HeroStageVariant {
  if (isEscalated) return "escalated";
  if (displayState === "blocked") return "blocked";
  if (displayState === "closed") return "closed";
  if (displayState === "in_progress" || displayState === "review_approved") {
    return "active";
  }
  return "default";
}

function heroStageLabel(
  task: GobbyTaskDetail,
  taskState: ReturnType<typeof getCanonicalTaskState>,
  displayState: TaskDisplayState,
  isEscalated: boolean,
): string {
  if (isEscalated) return "Escalated";
  if (taskState.is_closed) return "Closed";
  if (taskState.current_stage) return taskState.current_stage.display_name;
  if (task.expansion_status === "in_progress") return "Expanding";
  return TASK_STATE_LABELS[displayState];
}

export function TasksTabDetailPanel({
  task,
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
}: TasksTabDetailPanelProps) {
  const taskState = getCanonicalTaskState(task);
  const displayState = getTaskDisplayState(task);
  const ownerLabel = task.agent_name ?? taskState.owner_session_id ?? null;
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
      const childState = getTaskDisplayState(child);
      states[childState] = (states[childState] ?? 0) + 1;
    }
    return { subtaskBuckets: states, subtaskTotal: subtasks?.length ?? 0 };
  }, [subtasks]);

  const validationStatus = task.validation_status?.trim() || null;
  const validationFailCount = task.validation_fail_count ?? 0;
  const validationFeedback = task.validation_feedback?.trim() || null;
  const showValidationFeedback =
    validationFeedback !== null &&
    (validationStatus === "failed" || validationFailCount > 0);

  const isolation =
    task.isolation && task.isolation !== "none" ? task.isolation : null;
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

  const stageVariant = heroStageVariant(isEscalated, displayState);
  const stageLabel = heroStageLabel(task, taskState, displayState, isEscalated);
  const lastUpdated = task.updated_at ?? task.created_at ?? null;

  const hasRelationships =
    Boolean(parentTask) ||
    blockerCount > 0 ||
    blockingCount > 0 ||
    subtaskTotal > 0;
  const hasBody = Boolean(task.description) || Boolean(task.validation_criteria);
  const hasTrace =
    labels.length > 0 ||
    showAutomationRow ||
    commits.length > 0 ||
    (prUrl !== null && prLabel !== null) ||
    Boolean(task.closed_commit_sha) ||
    Boolean(task.closed_reason) ||
    Boolean(task.closed_in_session_id);

  return (
    <div className="activity-task-detail-card">
      {/* A. Hero. The pane bar above already renders the task ref + title;
          the hero leads with stage to keep "what's happening now" as the
          primary read. */}
      <header className="activity-task-detail-hero">
        <h2
          className={`activity-task-detail-hero__stage activity-task-detail-hero__stage--${stageVariant}`}
        >
          {stageLabel}
        </h2>
        <div className="activity-task-detail-hero__agent">
          {ownerLabel ? (
            <>
              <span>Driven by</span>
              <span
                className={`activity-task-detail-hero__agent-name${
                  ownerMono ? "" : ""
                }`}
              >
                {ownerLabel}
              </span>
            </>
          ) : (
            <span className="activity-task-detail-hero__agent-name activity-task-detail-hero__agent-name--unassigned">
              Unassigned
            </span>
          )}
          {lastUpdated && (
            <>
              <span className="activity-task-detail-hero__sep">·</span>
              <span>{relativeTime(lastUpdated)}</span>
            </>
          )}
        </div>
      </header>

      {/* B. Status surface */}
      {task.stages?.length > 0 && (
        <div className="activity-task-detail-status">
          <TaskStatusStrip task={task} compact />
        </div>
      )}

      {validationStatus && (
        <ValidationRow
          status={validationStatus}
          failCount={validationFailCount}
        />
      )}

      {/* Escalation */}
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
            <MetaKVRow label="From">{preEscalationStatus}</MetaKVRow>
          )}
          {escalationReason && (
            <div className="activity-task-detail-escalation-reason">
              {escalationReason}
            </div>
          )}
        </div>
      )}

      {/* C + D. Metadata + Relationships */}
      <div className="activity-task-detail-pair activity-task-detail-pair--meta">
        <section className="activity-task-detail-kv">
          <h3 className="activity-task-detail-kv__title">Metadata</h3>
          <MetaKVRow label="Claimed by" mono={ownerMono}>
            {ownerLabel ?? "Unassigned"}
          </MetaKVRow>
          {task.assigned_agent && (
            <MetaKVRow label="Agent" mono>
              {task.assigned_agent}
            </MetaKVRow>
          )}
          <MetaKVRow label="State">{stateLabel}</MetaKVRow>
          <MetaKVRow label="Category">{categoryLabel}</MetaKVRow>
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
          {task.path_cache && (
            <MetaKVRow label="Path" mono>
              {task.path_cache}
            </MetaKVRow>
          )}
        </section>

        {hasRelationships && (
          <section className="activity-task-detail-kv">
            <h3 className="activity-task-detail-kv__title">Relationships</h3>
            {parentTask && (
              <ParentKVRow parent={parentTask} onSelect={onSelectTask} />
            )}
            {(blockerCount > 0 || blockingCount > 0) && (
              <MetaKVRow label="Dependencies">
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
              </MetaKVRow>
            )}
            {subtaskTotal > 0 && (
              <MetaKVRow label={`Subtasks (${subtaskTotal})`}>
                <div className="activity-task-detail-pillrow">
                  {SUBTASK_STATE_ORDER.map((s) => {
                    const count = subtaskBuckets[s] ?? 0;
                    if (count === 0) return null;
                    return (
                      <span
                        key={s}
                        className="activity-task-detail-pill"
                        title={`${TASK_STATE_LABELS[s]} subtasks`}
                      >
                        <strong>{count}</strong>{" "}
                        {TASK_STATE_LABELS[s].toLowerCase()}
                      </span>
                    );
                  })}
                </div>
              </MetaKVRow>
            )}
          </section>
        )}
      </div>

      {/* E. Body */}
      {hasBody && (
        <div className="activity-task-detail-pair activity-task-detail-pair--body">
          {task.description && (
            <div className="activity-task-detail-section">
              <div className="activity-task-detail-section-title">
                Description
              </div>
              <div className="activity-task-detail-markdown message-content">
                <Markdown
                  content={task.description}
                  id={`task-desc-${task.id}`}
                />
              </div>
            </div>
          )}
          {task.validation_criteria && (
            <div className="activity-task-detail-section">
              <div className="activity-task-detail-section-title">
                Validation criteria
              </div>
              <div className="activity-task-detail-markdown message-content">
                <Markdown
                  content={task.validation_criteria}
                  id={`task-vc-${task.id}`}
                />
              </div>
            </div>
          )}
        </div>
      )}

      {showValidationFeedback && validationFeedback && (
        <div className="activity-task-detail-section activity-task-detail-section--failed">
          <div className="activity-task-detail-section-title">
            Validation feedback
          </div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown
              content={validationFeedback}
              id={`task-vf-${task.id}`}
            />
          </div>
        </div>
      )}

      {/* F. Trace */}
      {hasTrace && (
        <section className="activity-task-detail-kv">
          <h3 className="activity-task-detail-kv__title">Trace</h3>
          {labels.length > 0 && (
            <MetaKVRow label="Labels">
              <div className="activity-task-detail-pillrow">
                {labels.map((l, i) => (
                  <span
                    key={`${l}-${i}`}
                    className="activity-task-detail-label"
                  >
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
          {prUrl && prLabel && (
            <MetaKVRow label="PR" mono link href={prUrl}>
              {prLabel}
            </MetaKVRow>
          )}
          {task.closed_commit_sha && (
            <MetaKVRow
              label="Closing commit"
              mono
              title={task.closed_commit_sha}
            >
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
        </section>
      )}
    </div>
  );
}

function MetaKVRow({
  label,
  children,
  mono = false,
  link = false,
  href,
  title,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  link?: boolean;
  href?: string;
  title?: string;
}) {
  let valueCls = "activity-task-detail-kv-row__value";
  if (mono) valueCls += " activity-task-detail-kv-row__value--mono";
  if (link) valueCls += " activity-task-detail-kv-row__value--link";

  return (
    <div className="activity-task-detail-kv-row" title={title}>
      <span className="activity-task-detail-kv-row__label">{label}</span>
      {link && href ? (
        <a
          className={valueCls}
          href={href}
          target="_blank"
          rel="noopener noreferrer"
        >
          {children}
        </a>
      ) : (
        <span className={valueCls}>{children}</span>
      )}
    </div>
  );
}

function ValidationRow({
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
    <div
      className="activity-task-detail-validation-row"
      title="Validation status"
    >
      <span className="activity-task-detail-validation-row__label">
        Validation
      </span>
      <span className="activity-task-detail-validation-row__value">
        <span
          className={`activity-task-detail-pill activity-task-detail-pill--${variant}`}
        >
          {status}
        </span>
        {failCount > 0 && (
          <span className="activity-task-detail-validation-fails">
            {failCount} {failCount === 1 ? "fail" : "fails"}
          </span>
        )}
      </span>
    </div>
  );
}

function ParentKVRow({
  parent,
  onSelect,
}: {
  parent: ParentTaskRef;
  onSelect?: (id: string) => void;
}) {
  const handleClick = onSelect ? () => onSelect(parent.id) : undefined;
  return (
    <div className="activity-task-detail-kv-row" title="Parent task">
      <span className="activity-task-detail-kv-row__label">Parent</span>
      <span className="activity-task-detail-kv-row__value">
        {handleClick ? (
          <button
            type="button"
            className="activity-task-detail-parent-link"
            onClick={handleClick}
          >
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            <span className="activity-task-detail-parent-title">
              {parent.title}
            </span>
          </button>
        ) : (
          <>
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            {" "}
            <span className="activity-task-detail-parent-title">
              {parent.title}
            </span>
          </>
        )}
      </span>
    </div>
  );
}
