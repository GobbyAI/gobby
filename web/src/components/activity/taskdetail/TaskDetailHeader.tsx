import type { GobbyTaskDetail } from "../../../types/tasks";
import {
  PriorityBadge,
  TaskStateBadges,
  TypeBadge,
} from "../../tasks/TaskBadges";
import { DEFAULT_TASK_PRIORITY } from "../../../lib/taskOptions";
import { TaskTextField } from "../TaskFieldEditors";
import type { TaskInlineEditApi } from "./taskDetailFormat";

export interface TaskDetailHeaderProps {
  task: GobbyTaskDetail;
  edit?: TaskInlineEditApi;
}

/**
 * D5 §1 — the single source of state truth: ref · editable title ·
 * state/priority/type chips. Lifecycle state appears here and nowhere else
 * in the pane.
 */
export function TaskDetailHeader({ task, edit }: TaskDetailHeaderProps) {
  const titlePending = edit?.isFieldPending(task.id, "title") ?? false;

  return (
    <header className="flex flex-col gap-[0.55rem] border-b border-border bg-[var(--bg-primary)] px-4 pt-4 pb-[0.9rem]">
      <div className="flex items-center justify-between gap-[0.6rem]">
        <span
          className="shrink-0 font-mono text-[length:var(--text-xs)] font-[var(--font-weight-medium)] text-[var(--text-muted)]"
          data-task-detail-ref
        >
          {task.ref}
        </span>
        <div className="flex min-w-0 flex-wrap justify-end gap-[0.35rem]">
          <TaskStateBadges task={task} />
          <PriorityBadge priority={task.priority ?? DEFAULT_TASK_PRIORITY} />
          <TypeBadge type={task.task_type} />
        </div>
      </div>
      {edit ? (
        <TaskTextField
          value={task.title}
          ariaLabel="Task title"
          placeholder="Untitled task"
          disabled={titlePending}
          className="px-2 py-[0.3rem] text-[length:var(--text-xl)] font-[var(--font-weight-semibold)] tracking-[-0.01em]"
          onCommit={(value) =>
            edit.commitField({ task, field: "title", value })
          }
        />
      ) : (
        <h2 className="m-0 text-[length:var(--text-xl)] leading-[1.2] font-[var(--font-weight-semibold)] tracking-[-0.01em] [overflow-wrap:anywhere] text-[var(--text-primary)]">
          {task.title}
        </h2>
      )}
    </header>
  );
}
