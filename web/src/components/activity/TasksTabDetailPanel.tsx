import type {
  DependencyTree,
  GobbyTask,
  GobbyTaskDetail,
} from "../../types/tasks";
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
import { markdownBodyClassName } from "../shared/MarkdownBody";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";

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
    <div className="flex min-h-0 flex-col overflow-y-auto bg-[var(--bg-primary)]">
      <TaskDetailHeader task={task} edit={edit} />

      <TaskDetailStatusLine
        task={task}
        computed={computed}
        onClaim={onClaim}
        onRelease={onRelease}
        claimBusy={claimBusy}
      />

      {editError && (
        <div
          className="flex items-start gap-2 border-b border-[color-mix(in_srgb,var(--color-error)_35%,transparent)] bg-[var(--color-error-soft)] px-4 py-[0.55rem] text-[length:var(--text-sm)] text-[var(--color-error)] [&_span]:min-w-0 [&_span]:flex-auto"
          role="alert"
        >
          <span>{editError}</span>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={`shrink-0 cursor-pointer px-1 text-[length:var(--text-base)] leading-none text-inherit focus-visible:rounded-[0.2rem] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${coarseHitAreaCls} pointer-coarse:min-h-11 pointer-coarse:min-w-11`}
            aria-label="Dismiss error"
            onClick={() => edit?.clearError(task.id)}
          >
            ×
          </Button>
        </div>
      )}

      {computed.isEscalated && (
        <div
          className="border-b border-[color-mix(in_srgb,var(--color-error)_35%,transparent)] bg-[var(--color-error-soft)] px-4 py-[0.9rem]"
          data-task-detail-escalation
        >
          <div className="mb-[0.45rem] text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.08em] text-[var(--color-error)]">
            Escalated
            {task.escalated_at && (
              <span className="ml-[0.4rem] font-medium normal-case tracking-normal text-[var(--text-muted)]">
                {formatTaskDetailDate(task.escalated_at)}
              </span>
            )}
          </div>
          {preEscalationStatus && (
            <MetaKVRow label="From">{preEscalationStatus}</MetaKVRow>
          )}
          {escalationReason && (
            <div className="mt-2 whitespace-pre-wrap text-[length:var(--text-base)] leading-normal text-[var(--text-secondary)] [overflow-wrap:anywhere]">
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
        <div className="border-b border-border bg-[color-mix(in_srgb,var(--color-error)_6%,transparent)] px-4 py-[0.9rem]">
          <div className="mb-[0.45rem] text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.08em] text-[var(--color-error)]">
            Validation feedback
          </div>
          <div
            className={`message-content text-[length:var(--text-base)] leading-[1.6] text-[var(--text-secondary)] ${markdownBodyClassName}`}
          >
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
