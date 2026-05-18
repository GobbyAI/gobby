import type { GobbyTaskDetail } from "../../../hooks/useTasks";
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
export function TaskDetailHeader({
  task,
  edit,
}: TaskDetailHeaderProps) {
  const titlePending = edit?.isFieldPending(task.id, "title") ?? false;

  return (
    <header className="activity-task-detail-header">
      <div className="activity-task-detail-header__top">
        <span className="activity-task-detail-header__ref">{task.ref}</span>
        <div className="activity-task-detail-header__chips">
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
          onCommit={(value) =>
            edit.commitField({ task, field: "title", value })
          }
        />
      ) : (
        <h2 className="activity-task-detail-header__title">{task.title}</h2>
      )}
    </header>
  );
}
