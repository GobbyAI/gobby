import "./taskdetail/task-detail.css";

import type {
  DependencyTree,
  GobbyTask,
  GobbyTaskDetail,
} from "../../hooks/useTasks";
import { TaskDetailHeader } from "./taskdetail/TaskDetailHeader";
import { TaskDetailStatusLine } from "./taskdetail/TaskDetailStatusLine";
import { TaskDetailEditableCore } from "./taskdetail/TaskDetailEditableCore";
import { TaskDetailRelationships } from "./taskdetail/TaskDetailRelationships";
import { TaskDetailTrace } from "./taskdetail/TaskDetailTrace";
import { MetaKVRow, ValidationRow, type ParentTaskRef } from "./taskdetail/TaskDetailKV";
import {
  computeTaskDetail,
  formatTaskDetailDate,
  type TaskInlineEditApi,
} from "./taskdetail/taskDetailFormat";
import { Markdown } from "../chat/Markdown";

export type { GobbyTaskDetail };
export type { ParentTaskRef };
export type { TaskInlineEditApi };

interface TasksTabDetailPanelProps {
  task: GobbyTaskDetail;
  parentTask?: ParentTaskRef | null;
  onSelectTask?: (id: string) => void;
  dependencies?: DependencyTree | null;
  subtasks?: GobbyTask[];
  /** D4 inline-edit API, injected by the host. Read-only pane when absent. */
  edit?: TaskInlineEditApi;
  onClaim?: () => void;
  onRelease?: () => void;
  claimBusy?: boolean;
}

/**
 * D5 — the task detail pane, restructured into one honest read path:
 *
 *   1. Header        ref · editable title · state/priority/type chips
 *   2. Status line   stage · owner (once, friendly) · claim
 *   3. Escalation    only when escalated
 *   4. Editable core  category/priority/labels/description/criteria (D4)
 *   5. Relationships  parent · deps · subtasks, condensed
 *   6. Trace          collapsed by default; no "Path"
 *
 * State is shown exactly once (the header chips). The owner is a friendly
 * `#<ref>` shown exactly once (the status line) — never a raw UUID, never
 * doubled in a metadata block. Hierarchy is weight + space, no stripes.
 */
export function TasksTabDetailPanel({
  task,
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
  edit,
  onClaim,
  onRelease,
  claimBusy,
}: TasksTabDetailPanelProps) {
  const computed = computeTaskDetail(task);

  const validationStatus = task.validation_status?.trim() || null;
  const validationFailCount = task.validation_fail_count ?? 0;
  const validationFeedback = task.validation_feedback?.trim() || null;
  const showValidationFeedback =
    validationFeedback !== null &&
    (validationStatus === "failed" || validationFailCount > 0);

  const escalationReason = task.escalation_reason?.trim() || null;
  const preEscalationStatus = task.pre_escalation_status?.trim() || null;

  const editError = edit?.errorFor(task.id) ?? null;

  return (
    <div className="activity-task-detail-card">
      <TaskDetailHeader task={task} edit={edit} />

      <TaskDetailStatusLine
        task={task}
        computed={computed}
        onClaim={onClaim}
        onRelease={onRelease}
        claimBusy={claimBusy}
      />

      {editError && (
        <div className="activity-task-detail-edit-error" role="alert">
          <span>{editError}</span>
          <button
            type="button"
            className="activity-task-detail-edit-error__dismiss"
            aria-label="Dismiss error"
            onClick={() => edit?.clearError(task.id)}
          >
            ×
          </button>
        </div>
      )}

      {computed.isEscalated && (
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

      {validationStatus && (
        <ValidationRow
          status={validationStatus}
          failCount={validationFailCount}
        />
      )}

      <TaskDetailEditableCore task={task} edit={edit} />

      {showValidationFeedback && validationFeedback && (
        <div className="activity-task-detail-section activity-task-detail-section--failed">
          <div className="activity-task-detail-section-title">
            Validation feedback
          </div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown content={validationFeedback} id={`task-vf-${task.id}`} />
          </div>
        </div>
      )}

      <TaskDetailRelationships
        parentTask={parentTask}
        onSelectTask={onSelectTask}
        dependencies={dependencies}
        subtasks={subtasks}
      />

      <TaskDetailTrace task={task} />
    </div>
  );
}
