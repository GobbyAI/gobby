import type { DependencyTree, GobbyTask } from "../../hooks/useTasks";
import {
  TasksTabDetailPanel,
  type GobbyTaskDetail,
  type ParentTaskRef,
  type TaskInlineEditApi,
} from "./TasksTabDetailPanel";

interface TasksTabDetailSectionProps {
  headerRef: string | null;
  loading: boolean;
  task: GobbyTaskDetail | null;
  parentTask: ParentTaskRef | null;
  onSelectTask: (id: string) => void;
  dependencies: DependencyTree | null;
  subtasks: GobbyTask[];
  edit: TaskInlineEditApi;
  onClaim?: () => void;
  onRelease: () => void;
  claimBusy: boolean;
}

/**
 * List-mode detail region: pane chrome + loading / detail / not-found. Lifted
 * out of `TasksTab` so the tab stays a thin composer (CLAUDE.md rule 2). The
 * host renders this only when a task is selected; Board mode has no detail
 * pane.
 */
export function TasksTabDetailSection({
  headerRef,
  loading,
  task,
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
  edit,
  onClaim,
  onRelease,
  claimBusy,
}: TasksTabDetailSectionProps) {
  return (
    <div className="activity-task-detail-shell">
      <div className="activity-task-pane-bar activity-task-pane-bar--detail">
        <span className="activity-task-pane-bar__title">
          Task {headerRef ?? "—"}
        </span>
      </div>
      {loading ? (
        <p className="activity-task-detail-loading">Loading...</p>
      ) : task ? (
        <TasksTabDetailPanel
          task={task}
          parentTask={parentTask}
          onSelectTask={onSelectTask}
          dependencies={dependencies}
          subtasks={subtasks}
          edit={edit}
          onClaim={onClaim}
          onRelease={onRelease}
          claimBusy={claimBusy}
        />
      ) : (
        <p className="activity-task-detail-empty">Task not found</p>
      )}
    </div>
  );
}
